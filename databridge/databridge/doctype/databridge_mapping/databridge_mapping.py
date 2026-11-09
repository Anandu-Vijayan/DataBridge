from __future__ import annotations

import frappe
from frappe import _
from frappe.model import no_value_fields
from frappe.model.document import Document

from databridge.engine.mapping import build_snapshot, spec_from_snapshot, validate_rules


def target_field_names(doctype: str) -> set[str]:
	"""Fields a mapping may write to: real value fields plus the document name."""
	meta = frappe.get_meta(doctype)
	names = {df.fieldname for df in meta.fields if df.fieldtype not in no_value_fields and not df.is_virtual}
	return names | {"name"}


class DataBridgeMapping(Document):
	def validate(self) -> None:
		meta = frappe.get_meta(self.target_doctype)
		if meta.istable or meta.issingle or meta.is_virtual:
			frappe.throw(_("Target DocType must be a regular document type"))

		connector = frappe.db.get_value("DataBridge Connection", self.connection, "connector")
		spec = spec_from_snapshot(build_snapshot(self.as_dict()))
		problems = validate_rules(
			spec.rules,
			target_field_names(self.target_doctype),
			spec.mode,
			spec.match_field,
			source_id_required=connector != "MySQL",
			source_id_field=spec.source_id_field,
		)
		if problems:
			frappe.throw(
				"<br>".join(frappe.utils.escape_html(p) for p in problems), title=_("Invalid mapping")
			)
