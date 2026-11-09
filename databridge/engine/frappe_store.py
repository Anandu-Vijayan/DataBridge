"""Frappe adapter for the runner: row locks, job/record/log persistence and the "long" queue.

This is the only module that wires engine.runner to the database. Internal bookkeeping DocTypes
(job records, logs, references) are written with ignore_permissions because the run's user may not
hold a role on them. Target DocTypes are never written here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from databridge.connectors.base import ConnectionConfig
from databridge.connectors.registry import build_connector
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import MappingSpec, spec_from_snapshot
from databridge.engine.runner import (
	COUNTER_KEYS,
	FailedRecord,
	JobState,
	RecordResult,
	fail_job,
	process_batch,
)
from databridge.engine.writer import FrappeRecordProcessor
from databridge.utils.files import resolve_private_file_path
from databridge.utils.redact import redact_text

JOB = "DataBridge Job"
RECORD = "DataBridge Job Record"
LOG = "DataBridge Execution Log"
CONNECTION = "DataBridge Connection"
RUN_BATCH_METHOD = "databridge.engine.frappe_store.run_batch"
BATCH_TIMEOUT = 1500

_STATE_FIELDS = [
	"status", "stop_requested", "phase", "dry_run", "submit_documents", "batch_size",
	"mapping_snapshot", "checkpoint", "current_batch", "run_as", *COUNTER_KEYS,
]  # fmt: skip


def load_connection_config(connection: str) -> ConnectionConfig:
	"""Resolve a Connection document into a ConnectionConfig. The password stays out of every repr."""
	import frappe

	doc = frappe.get_doc(CONNECTION, connection)
	options: dict[str, Any] = {
		"has_header": doc.has_header,
		"encoding": doc.encoding or "utf-8",
		"delimiter": doc.delimiter or ",",
		"sheet_name": doc.sheet_name,
		"record_tag": doc.record_tag,
		"host": doc.host,
		"port": doc.port,
		"database": doc.database,
		"username": doc.username,
		"use_ssl": doc.use_ssl,
	}
	if doc.extra_options:
		try:
			extra = json.loads(doc.extra_options)
		except ValueError:
			raise DataBridgeError(errors.INVALID_INPUT, "Extra options is not valid JSON") from None
		if isinstance(extra, dict):
			options.update({k: v for k, v in extra.items() if k not in options})
	if doc.connector != "MySQL":
		options["path"] = resolve_private_file_path(doc.source_file)
	password = doc.get_password("password", raise_exception=False) or ""
	return ConnectionConfig(connector=doc.connector, options=options, password=password)


class FrappeJobStore:
	def __init__(self, secrets: list[str] | None = None) -> None:
		self.secrets = secrets or []
		self._savepoints = 0
		self._target_doctype = ""

	# ------------------------------------------------------------------ state
	def lock_state(self, job: str) -> JobState:
		import frappe

		# A locking read always sees the latest committed row, even under REPEATABLE READ.
		row = frappe.db.get_value(JOB, job, _STATE_FIELDS, as_dict=True, for_update=True)
		if not row:
			raise DataBridgeError(errors.NOT_FOUND, f"Job {job} does not exist")
		snapshot = json.loads(row.mapping_snapshot) if row.mapping_snapshot else {}
		self._target_doctype = snapshot.get("target_doctype", "")
		return JobState(
			name=job,
			status=row.status,
			stop_requested=row.stop_requested or "",
			phase=row.phase or "migrate",
			dry_run=bool(row.dry_run),
			submit=bool(row.submit_documents),
			batch_size=int(row.batch_size or 200),
			snapshot=snapshot,
			checkpoint=json.loads(row.checkpoint) if row.checkpoint else None,
			counters={k: int(row.get(k) or 0) for k in COUNTER_KEYS},
			current_batch=int(row.current_batch or 0),
			run_as=row.run_as or "",
		)

	def check_permissions(self, spec: MappingSpec, state: JobState) -> None:
		import frappe

		user = frappe.session.user
		needed: set[str] = set()
		if spec.mode in ("Insert", "Skip", "Upsert") or state.dry_run:
			needed.add("create")
		if spec.mode in ("Update", "Upsert"):
			needed.add("write")
		if state.submit:
			needed.add("submit")
		if not frappe.db.exists("DocType", spec.target_doctype):
			raise DataBridgeError(
				errors.NOT_FOUND, f"Target DocType {spec.target_doctype!r} no longer exists"
			)
		for ptype in sorted(needed):
			if not frappe.has_permission(spec.target_doctype, ptype, user=user):
				raise DataBridgeError(
					errors.PERMISSION_DENIED, f"{user} has no {ptype} permission on {spec.target_doctype}"
				)

	def save_progress(self, state: JobState, **changes: Any) -> None:
		import frappe

		values: dict[str, Any] = {}
		for key in ("status", "phase", "stop_requested", "last_error", "current_batch"):
			if key in changes:
				values[key] = changes[key]
				setattr(state, key, changes[key])
		if "counters" in changes:
			values.update(changes["counters"])
			state.counters = dict(changes["counters"])
		if "checkpoint" in changes:
			values["checkpoint"] = json.dumps(changes["checkpoint"])
			state.checkpoint = changes["checkpoint"]
		if changes.get("started"):
			values["started_at"] = frappe.utils.now_datetime()
		if changes.get("ended"):
			values["ended_at"] = frappe.utils.now_datetime()
		if values:
			frappe.db.set_value(JOB, state.name, values)

	# ------------------------------------------------------------------ transaction control
	@contextmanager
	def savepoint(self) -> Iterator[None]:
		import frappe

		self._savepoints += 1
		name = f"databridge_rec_{self._savepoints}"
		frappe.db.savepoint(name)
		try:
			yield
		except BaseException:
			frappe.db.rollback(save_point=name)
			raise
		frappe.db.release_savepoint(name)

	def rollback(self) -> None:
		import frappe

		frappe.db.rollback()

	def commit(self) -> None:
		import frappe

		# One batch is one transaction: target docs, references and records commit together here.
		frappe.db.commit()  # nosemgrep

	# ------------------------------------------------------------------ records and logs
	def _stage(self, data: dict[str, Any] | None) -> str | None:
		if not data:
			return None
		return redact_text(json.dumps(data, default=str), self.secrets)

	def add_record(self, job: str, batch_no: int, result: RecordResult) -> None:
		import frappe

		frappe.get_doc(
			{
				"doctype": RECORD,
				"job": job,
				"batch_no": batch_no,
				"source_id": result.source_id[:140],
				"status": result.status,
				"action": result.action,
				"target_doctype": self._target_doctype,
				"target_name": result.target_name,
				"error_code": result.error_code,
				"error_message": redact_text(result.error_message, self.secrets),
				"source_data": self._stage(result.source_data) if result.status == "Failed" else None,
			}
		).insert(ignore_permissions=True)
		frappe.clear_messages()

	def update_record(self, record: str, result: RecordResult, retry_count: int) -> None:
		import frappe

		frappe.db.set_value(
			RECORD,
			record,
			{
				"status": result.status,
				"action": result.action,
				"target_name": result.target_name,
				"error_code": result.error_code,
				"error_message": redact_text(result.error_message, self.secrets),
				"retry_count": retry_count,
				"source_data": self._stage(result.source_data) if result.status == "Failed" else None,
			},
			update_modified=False,
		)
		frappe.clear_messages()

	def failed_records(self, job: str, after: str, limit: int) -> list[FailedRecord]:
		import frappe

		rows = frappe.get_all(
			RECORD,
			filters={"job": job, "status": "Failed", "name": (">", after)},
			fields=["name", "source_id", "source_data", "retry_count"],
			order_by="name asc",
			page_length=limit,
		)
		return [
			FailedRecord(
				name=r.name,
				source_id=r.source_id or "",
				source_data=json.loads(r.source_data) if r.source_data else None,
				retry_count=int(r.retry_count or 0),
			)
			for r in rows
		]

	def log(
		self,
		job: str,
		level: str,
		event: str,
		message: str,
		batch_no: int | None = None,
		details: dict | None = None,
	) -> None:
		import frappe

		frappe.get_doc(
			{
				"doctype": LOG,
				"job": job,
				"level": level,
				"event": event,
				"batch_no": batch_no,
				"message": redact_text(message, self.secrets)[:1000],
				"details": json.dumps(details) if details else None,
			}
		).insert(ignore_permissions=True)

	def enqueue_next(self, job: str, batch_no: int) -> None:
		enqueue_batch(job, batch_no)


def enqueue_batch(job: str, batch_no: int, after_commit: bool = False) -> None:
	"""Queue the next single-batch job. Deduplicated by job id so a double enqueue cannot double-run."""
	import frappe

	frappe.enqueue(
		RUN_BATCH_METHOD,
		queue="long",
		timeout=BATCH_TIMEOUT,
		job_id=f"databridge::{job}::{batch_no}",
		deduplicate=True,
		enqueue_after_commit=after_commit,
		job_name=f"databridge {job} batch {batch_no}",
		job=job,
	)


def run_batch(job: str) -> None:
	"""Background entry point: process exactly one batch of one job."""
	import frappe

	run_as, snapshot_json = frappe.db.get_value(JOB, job, ["run_as", "mapping_snapshot"]) or (None, None)
	if not run_as or not snapshot_json:
		return
	# Target writes must run with the permissions of the user who started the job.
	frappe.set_user(run_as)  # nosemgrep

	store = FrappeJobStore()
	try:
		spec = spec_from_snapshot(json.loads(snapshot_json))
		config = load_connection_config(spec.connection)
		store.secrets = config.secrets()
		connector = build_connector(config)
	except Exception as exc:
		state = store.lock_state(job)
		fail_job(store, state, exc, store.secrets)
		return

	def processor_factory(spec: MappingSpec, state: JobState) -> FrappeRecordProcessor:
		return FrappeRecordProcessor(spec, job=job, dry_run=state.dry_run, submit=state.submit)

	result = process_batch(job, store, connector, processor_factory, store.secrets)
	if result in ("completed", "failed", "cancelled", "paused"):
		frappe.publish_realtime(
			"databridge_job_update", {"job": job, "result": result}, doctype=JOB, docname=job
		)
