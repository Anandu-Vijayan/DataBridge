from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

FILE_CONNECTORS = ("CSV", "TSV", "XLSX", "JSON", "JSONL", "XML")


class DataBridgeConnection(Document):
	def validate(self) -> None:
		if self.connector in FILE_CONNECTORS:
			self.host = self.database = self.username = None
		elif self.connector == "MySQL":
			if self.port and not (1 <= int(self.port) <= 65535):
				frappe.throw(_("Port must be between 1 and 65535"))
		if self.has_value_changed("connector") or self.has_value_changed("source_file"):
			self.status = "Untested"
			self.last_error = None
