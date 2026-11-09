from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class DataBridgeMapping(Document):
	def validate(self) -> None:
		meta = frappe.get_meta(self.target_doctype)
		if meta.istable or meta.issingle or meta.is_virtual:
			frappe.throw(_("Target DocType must be a regular document type"))
