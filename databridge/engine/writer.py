"""Writing mapped records to the target DocType, only through the Frappe document lifecycle.

decide() is pure. FrappeRecordProcessor is the edge: it checks the reference first, then the
target, validates, and then inserts/saves/submits with the session user's own permissions. It never
passes ignore_permissions for a target write and never runs raw SQL against target tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import MappedRecord, MappingSpec
from databridge.engine.references import find_reference, make_ref_key, save_reference
from databridge.engine.validator import FieldSpec, build_field_specs, validate_values
from databridge.utils.redact import redact_text

INSERT = "insert"
UPDATE = "update"
SKIP = "skip"
FAIL = "fail"


@dataclass(frozen=True)
class WriteOutcome:
	status: str  # Job Record status: Validated, Inserted, Updated, Skipped
	action: str
	target_name: str | None = None
	message: str | None = None


def decide(mode: str, target_exists: bool) -> str:
	"""Pure decision table. The reference/target lookup happens before this is called."""
	if mode == "Insert":
		return SKIP if target_exists else INSERT
	if mode == "Skip":
		return SKIP if target_exists else INSERT
	if mode == "Update":
		return UPDATE if target_exists else FAIL
	if mode == "Upsert":
		return UPDATE if target_exists else INSERT
	raise DataBridgeError(errors.MAPPING_INVALID, f"Unknown mode {mode!r}")


def translate_exception(exc: BaseException, secrets: list[str] | None = None) -> DataBridgeError:
	"""Map a Frappe exception to a stable DataBridge error code with a clean, redacted message."""
	import frappe
	from frappe.utils import strip_html

	if isinstance(exc, DataBridgeError):
		return exc
	if isinstance(exc, frappe.PermissionError):
		code = errors.PERMISSION_DENIED
	elif isinstance(exc, frappe.DuplicateEntryError):
		code = errors.DUPLICATE_TARGET
	elif isinstance(exc, frappe.MandatoryError):
		code = errors.REQUIRED_FIELD_MISSING
	elif isinstance(exc, frappe.LinkValidationError):
		code = errors.LINK_NOT_FOUND
	else:
		code = errors.WRITE_FAILED
	message = redact_text(strip_html(str(exc)) or type(exc).__name__, secrets or [])
	return DataBridgeError(code, message[:1000])


class FrappeRecordProcessor:
	"""Validates and (unless dry_run) writes one mapped record. One instance per batch."""

	def __init__(self, spec: MappingSpec, *, job: str, dry_run: bool, submit: bool) -> None:
		import frappe

		self.spec = spec
		self.job = job
		self.dry_run = dry_run
		self.submit = submit
		self.meta = frappe.get_meta(spec.target_doctype)
		self.field_specs: dict[str, FieldSpec] = build_field_specs(self.meta)
		self._link_cache: dict[tuple[str, str], bool] = {}

	def _link_exists(self, doctype: str, name: str) -> bool:
		import frappe

		key = (doctype, name)
		if key not in self._link_cache:
			self._link_cache[key] = bool(frappe.db.exists(doctype, name))
		return self._link_cache[key]

	def _find_target(self, ref: dict[str, Any] | None, values: dict[str, Any]) -> str | None:
		"""Reference first, then the match field. Returns the existing target name or None."""
		import frappe

		doctype = self.spec.target_doctype
		if ref and frappe.db.exists(doctype, ref["target_name"]):
			return ref["target_name"]
		match_field = self.spec.match_field
		if not match_field:
			return None
		match_value = values.get(match_field)
		if match_value in (None, ""):
			if self.spec.mode == "Update":
				raise DataBridgeError(errors.VALIDATION_ERROR, f"{match_field}: match field is empty")
			return None
		if match_field == "name":
			return str(match_value) if frappe.db.exists(doctype, str(match_value)) else None
		return frappe.db.get_value(doctype, {match_field: match_value}, "name")

	def process(self, mapped: MappedRecord) -> WriteOutcome:
		spec = self.spec
		ref_key = make_ref_key(spec.connection, spec.source_object, mapped.source_id, spec.target_doctype)
		ref = find_reference(ref_key)
		target = self._find_target(ref, mapped.values)
		decision = decide(spec.mode, target is not None)
		if decision == FAIL:
			raise DataBridgeError(errors.TARGET_NOT_FOUND, "No existing document matches this record")

		values = validate_values(
			mapped.values,
			self.field_specs,
			self._link_exists,
			check_required=decision == INSERT,
		)
		if self.dry_run:
			status = {INSERT: "Validated", UPDATE: "Validated", SKIP: "Skipped"}[decision]
			return WriteOutcome(status, decision, target, "Dry run, nothing written")

		try:
			return self._write(decision, ref, ref_key, target, values, mapped.source_id)
		except DataBridgeError:
			raise
		except Exception as exc:
			raise translate_exception(exc) from None

	def _write(
		self,
		decision: str,
		ref: dict[str, Any] | None,
		ref_key: str,
		target: str | None,
		values: dict[str, Any],
		source_id: str,
	) -> WriteOutcome:
		import frappe

		spec = self.spec
		if decision == SKIP:
			# Record the link so later runs find it by reference instead of by match field.
			if target and not ref:
				self._save_ref(ref, ref_key, source_id, target)
			return WriteOutcome("Skipped", SKIP, target, "Target already exists")

		if decision == UPDATE:
			doc = frappe.get_doc(spec.target_doctype, target)
			doc.update({k: v for k, v in values.items() if k != "name"})
			doc.save()  # checks write permission for the session user
			status = "Updated"
		else:
			payload = {k: v for k, v in values.items() if v is not None}
			doc = frappe.get_doc({"doctype": spec.target_doctype, **payload})
			doc.insert()  # checks create permission for the session user
			status = "Inserted"

		if self.submit and doc.docstatus == 0:
			if not self.meta.is_submittable:
				raise DataBridgeError(errors.SUBMIT_NOT_ALLOWED, f"{spec.target_doctype} is not submittable")
			if not frappe.has_permission(spec.target_doctype, "submit", doc=doc):
				raise DataBridgeError(
					errors.SUBMIT_NOT_ALLOWED, "You do not have permission to submit this document"
				)
			doc.submit()

		self._save_ref(ref, ref_key, source_id, doc.name)
		return WriteOutcome(status, decision, doc.name)

	def _save_ref(self, ref: dict[str, Any] | None, ref_key: str, source_id: str, target_name: str) -> None:
		save_reference(
			ref_key=ref_key,
			existing=ref,
			connection=self.spec.connection,
			source_object=self.spec.source_object,
			source_id=source_id,
			target_doctype=self.spec.target_doctype,
			target_name=target_name,
			job=self.job,
		)
