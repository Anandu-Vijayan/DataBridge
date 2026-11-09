"""Job control APIs. Every call checks permissions, takes the job row lock, and follows the state machine."""

import json

import frappe

from databridge.api.connection import _open
from databridge.databridge.doctype.databridge_mapping.databridge_mapping import target_field_names
from databridge.engine import errors
from databridge.engine.control import require_transition
from databridge.engine.errors import DataBridgeError
from databridge.engine.frappe_store import enqueue_batch
from databridge.engine.mapping import build_snapshot, spec_from_snapshot, validate_rules
from databridge.engine.runner import COUNTER_KEYS
from databridge.utils.response import api_endpoint, clamp_page, require_str

JOB = "DataBridge Job"
RECORD = "DataBridge Job Record"
LOG = "DataBridge Execution Log"
RECORD_STATUSES = ("Pending", "Validated", "Inserted", "Updated", "Skipped", "Failed")


def _locked_job(job: str, ptype: str):
	"""Load a job, check the caller's permission and lock its row against the worker."""
	name = require_str(job, "job")
	doc = frappe.get_doc(JOB, name)
	doc.check_permission(ptype)
	frappe.db.get_value(JOB, name, "status", for_update=True)
	doc.reload()
	return doc


def _log(job: str, level: str, event: str, message: str) -> None:
	frappe.get_doc({"doctype": LOG, "job": job, "level": level, "event": event, "message": message}).insert(
		ignore_permissions=True  # bookkeeping DocType, not a migration target
	)


def _check_target_permissions(doctype: str, mode: str, dry_run: bool, submit: bool) -> None:
	needed = set()
	if mode in ("Insert", "Skip", "Upsert") or dry_run:
		needed.add("create")
	if mode in ("Update", "Upsert"):
		needed.add("write")
	if submit:
		needed.add("submit")
	for ptype in sorted(needed):
		if not frappe.has_permission(doctype, ptype):
			raise DataBridgeError(errors.PERMISSION_DENIED, f"You have no {ptype} permission on {doctype}")


@frappe.whitelist(methods=["POST"])
@api_endpoint
def start_job(job: str, dry_run: int | None = None) -> dict:
	"""Validate, freeze the mapping, and queue the first batch."""
	doc = _locked_job(job, "write")
	require_transition("start", doc.status)
	if dry_run is not None:
		doc.dry_run = 1 if int(dry_run) else 0
	if doc.dry_run:
		doc.submit_documents = 0

	mapping = frappe.get_doc("DataBridge Mapping", doc.mapping)
	mapping.check_permission("read")
	target = mapping.target_doctype
	_check_target_permissions(target, doc.mode, bool(doc.dry_run), bool(doc.submit_documents))
	if doc.submit_documents and not frappe.get_meta(target).is_submittable:
		raise DataBridgeError(errors.SUBMIT_NOT_ALLOWED, f"{target} is not submittable")

	snapshot = build_snapshot(mapping.as_dict(), doc.mode)
	spec = spec_from_snapshot(snapshot)
	connector_type = frappe.db.get_value("DataBridge Connection", mapping.connection, "connector")
	problems = validate_rules(
		spec.rules,
		target_field_names(target),
		spec.mode,
		spec.match_field,
		source_id_required=connector_type != "MySQL",
		source_id_field=spec.source_id_field,
	)
	if problems:
		raise DataBridgeError(errors.MAPPING_INVALID, "; ".join(problems)[:900])

	_, connector, _ = _open(mapping.connection, "read")
	connector.test()  # fail now, not in the background

	values = {
		"status": "Queued",
		"dry_run": doc.dry_run,
		"submit_documents": doc.submit_documents,
		"mapping_snapshot": json.dumps(snapshot),
		"checkpoint": None,
		"phase": "migrate",
		"stop_requested": "",
		"current_batch": 0,
		"run_as": frappe.session.user,
		"started_at": None,
		"ended_at": None,
		"last_error": None,
		**dict.fromkeys(COUNTER_KEYS, 0),
	}
	frappe.db.set_value(JOB, doc.name, values)
	_log(doc.name, "Info", "job_queued", f"Queued by {frappe.session.user}, dry run {bool(doc.dry_run)}")
	enqueue_batch(doc.name, 1, after_commit=True)
	return {"job": doc.name, "status": "Queued"}


@frappe.whitelist(methods=["POST"])
@api_endpoint
def pause_job(job: str) -> dict:
	"""Pause after the current batch finishes."""
	doc = _locked_job(job, "write")
	require_transition("pause", doc.status)
	frappe.db.set_value(JOB, doc.name, "stop_requested", "Pause")
	_log(doc.name, "Info", "pause_requested", f"Pause requested by {frappe.session.user}")
	return {"job": doc.name, "stop_requested": "Pause"}


@frappe.whitelist(methods=["POST"])
@api_endpoint
def resume_job(job: str) -> dict:
	"""Continue a paused or failed job from its last checkpoint."""
	doc = _locked_job(job, "write")
	require_transition("resume", doc.status)
	_check_target_permissions(
		json.loads(doc.mapping_snapshot)["target_doctype"],
		doc.mode,
		bool(doc.dry_run),
		bool(doc.submit_documents),
	)
	frappe.db.set_value(
		JOB,
		doc.name,
		{"status": "Queued", "stop_requested": "", "run_as": frappe.session.user, "last_error": None},
	)
	_log(doc.name, "Info", "job_resumed", f"Resumed by {frappe.session.user}")
	enqueue_batch(doc.name, int(doc.current_batch or 0) + 1, after_commit=True)
	return {"job": doc.name, "status": "Queued"}


@frappe.whitelist(methods=["POST"])
@api_endpoint
def cancel_job(job: str) -> dict:
	"""Cancel after the current batch. A paused job is cancelled immediately."""
	doc = _locked_job(job, "write")
	require_transition("cancel", doc.status)
	if doc.status == "Paused":
		frappe.db.set_value(JOB, doc.name, {"status": "Cancelled", "ended_at": frappe.utils.now_datetime()})
		result = "Cancelled"
	else:
		frappe.db.set_value(JOB, doc.name, "stop_requested", "Cancel")
		result = "Cancel requested"
	_log(doc.name, "Warning", "cancel_requested", f"Cancel requested by {frappe.session.user}")
	return {"job": doc.name, "result": result}


@frappe.whitelist(methods=["POST"])
@api_endpoint
def retry_failed(job: str) -> dict:
	"""Re-run only the failed records of a finished job, using its original mapping snapshot."""
	doc = _locked_job(job, "write")
	require_transition("retry", doc.status)
	if doc.dry_run:
		raise DataBridgeError(errors.INVALID_STATE, "Dry runs cannot be retried. Create a real run instead.")
	if not int(doc.failed or 0):
		raise DataBridgeError(errors.INVALID_STATE, "There are no failed records to retry")
	_check_target_permissions(
		json.loads(doc.mapping_snapshot)["target_doctype"], doc.mode, False, bool(doc.submit_documents)
	)
	frappe.db.set_value(
		JOB,
		doc.name,
		{
			"status": "Queued",
			"phase": "retry",
			"checkpoint": json.dumps({"after": ""}),
			"current_batch": 0,
			"stop_requested": "",
			"run_as": frappe.session.user,
			"last_error": None,
		},
	)
	_log(
		doc.name,
		"Info",
		"retry_queued",
		f"Retry of {doc.failed} failed records queued by {frappe.session.user}",
	)
	enqueue_batch(doc.name, 1, after_commit=True)
	return {"job": doc.name, "status": "Queued"}


@frappe.whitelist(methods=["POST"])
@api_endpoint
def create_real_run(job: str) -> dict:
	"""Copy a finished dry run into a new Draft job that will really write."""
	name = require_str(job, "job")
	source = frappe.get_doc(JOB, name)
	source.check_permission("read")
	if not source.dry_run:
		raise DataBridgeError(errors.INVALID_STATE, "Only a dry run can be turned into a real run")
	if source.status not in ("Completed", "Completed With Errors"):
		raise DataBridgeError(errors.INVALID_STATE, "Wait for the dry run to finish first")
	new = frappe.get_doc(
		{
			"doctype": JOB,
			"title": f"{source.title or source.mapping} (real run)",
			"mapping": source.mapping,
			"mode": source.mode,
			"batch_size": source.batch_size,
			"dry_run": 0,
			"submit_documents": 0,
		}
	)
	new.insert()  # normal permission checks apply
	return {"job": new.name}


@frappe.whitelist(methods=["GET"])
@api_endpoint
def get_job_progress(job: str) -> dict:
	"""Status and counters for one job."""
	name = require_str(job, "job")
	doc = frappe.get_doc(JOB, name)
	doc.check_permission("read")
	return {
		"job": doc.name,
		"status": doc.status,
		"phase": doc.phase,
		"stop_requested": doc.stop_requested or "",
		"dry_run": int(doc.dry_run or 0),
		"current_batch": int(doc.current_batch or 0),
		"counters": {k: int(doc.get(k) or 0) for k in COUNTER_KEYS},
		"last_error": doc.last_error,
		"started_at": doc.started_at,
		"ended_at": doc.ended_at,
	}


@frappe.whitelist(methods=["GET"])
@api_endpoint
def get_job_records(job: str, status: str | None = None, start: int = 0, page_length: int = 20) -> dict:
	"""Paginated per-record outcomes. source_data is left out to keep payloads small."""
	name = require_str(job, "job")
	frappe.get_doc(JOB, name).check_permission("read")
	start, page_length = clamp_page(start, page_length)
	filters: dict = {"job": name}
	if status:
		if status not in RECORD_STATUSES:
			raise DataBridgeError(errors.INVALID_INPUT, f"status must be one of {', '.join(RECORD_STATUSES)}")
		filters["status"] = status
	rows = frappe.get_all(
		RECORD,
		filters=filters,
		fields=["name", "batch_no", "source_id", "status", "action", "target_doctype", "target_name",
			"error_code", "error_message", "retry_count"],
		order_by="creation asc, name asc",
		start=start,
		page_length=page_length,
	)  # fmt: skip
	return {
		"rows": rows,
		"total": frappe.db.count(RECORD, filters),
		"start": start,
		"page_length": page_length,
	}


@frappe.whitelist(methods=["GET"])
@api_endpoint
def get_job_logs(job: str, start: int = 0, page_length: int = 50) -> dict:
	"""Paginated execution log for one job."""
	name = require_str(job, "job")
	frappe.get_doc(JOB, name).check_permission("read")
	start, page_length = clamp_page(start, page_length)
	rows = frappe.get_all(
		LOG,
		filters={"job": name},
		fields=["creation", "level", "event", "batch_no", "message"],
		order_by="creation asc, name asc",
		start=start,
		page_length=page_length,
	)
	return {
		"rows": rows,
		"total": frappe.db.count(LOG, {"job": name}),
		"start": start,
		"page_length": page_length,
	}
