"""Mapping APIs: target field discovery, suggestions and validation."""

import frappe

from databridge.databridge.doctype.databridge_mapping.databridge_mapping import target_field_names
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import build_snapshot, spec_from_snapshot, suggest_rules, validate_rules
from databridge.utils.response import api_endpoint, require_str

MAPPING = "DataBridge Mapping"


def _require_read(doctype: str) -> str:
	doctype = require_str(doctype, "doctype")
	if not frappe.db.exists("DocType", doctype):
		raise DataBridgeError(errors.NOT_FOUND, f"DocType {doctype!r} does not exist")
	if not frappe.has_permission(doctype, "read"):
		raise DataBridgeError(errors.PERMISSION_DENIED, f"No read permission on {doctype}")
	return doctype


def _target_labels(doctype: str) -> dict[str, str]:
	meta = frappe.get_meta(doctype)
	allowed = target_field_names(doctype)
	labels = {df.fieldname: df.label or df.fieldname for df in meta.fields if df.fieldname in allowed}
	labels["name"] = "ID"
	return labels


@frappe.whitelist(methods=["GET"])
@api_endpoint
def get_target_fields(doctype: str) -> list:
	"""Writable value fields of a target DocType, with type and required flag."""
	doctype = _require_read(doctype)
	meta = frappe.get_meta(doctype)
	allowed = target_field_names(doctype)
	rows = [
		{
			"fieldname": df.fieldname,
			"label": df.label or df.fieldname,
			"fieldtype": df.fieldtype,
			"reqd": int(bool(df.reqd)),
			"options": df.options if df.fieldtype in ("Link", "Select") else "",
		}
		for df in meta.fields
		if df.fieldname in allowed
	]
	rows.insert(0, {"fieldname": "name", "label": "ID", "fieldtype": "Data", "reqd": 0, "options": ""})
	return rows


@frappe.whitelist(methods=["GET"])
@api_endpoint
def suggest_mapping(connection: str, source_object: str, target_doctype: str) -> list:
	"""Propose field rows by exact normalised name/label match. Never guesses."""
	from databridge.api.connection import _open

	doctype = _require_read(target_doctype)
	_, connector, _ = _open(connection, "read")
	source_fields = connector.list_fields(source_object or "default")
	return suggest_rules(source_fields, _target_labels(doctype))


@frappe.whitelist(methods=["GET"])
@api_endpoint
def validate_mapping(mapping: str) -> dict:
	"""Run the same checks as saving the mapping and report every problem."""
	name = require_str(mapping, "mapping")
	doc = frappe.get_doc(MAPPING, name)
	doc.check_permission("read")
	spec = spec_from_snapshot(build_snapshot(doc.as_dict()))
	connector = frappe.db.get_value("DataBridge Connection", doc.connection, "connector")
	problems = validate_rules(
		spec.rules,
		target_field_names(doc.target_doctype),
		spec.mode,
		spec.match_field,
		source_id_required=connector != "MySQL",
		source_id_field=spec.source_id_field,
	)
	return {"valid": not problems, "problems": problems}
