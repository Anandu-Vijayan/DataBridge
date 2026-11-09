from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

MAX_BATCH_SIZE = 1000
RUNNING_STATES = ("Queued", "Running")


class DataBridgeJob(Document):
	def validate(self) -> None:
		self.batch_size = int(self.batch_size or 200)
		if not 1 <= self.batch_size <= MAX_BATCH_SIZE:
			frappe.throw(_("Batch size must be between 1 and {0}").format(MAX_BATCH_SIZE))
		if self.dry_run and self.submit_documents:
			frappe.throw(_("A dry run cannot submit documents"))
		if not self.is_new() and self.status in RUNNING_STATES:
			locked = ("mapping", "mode", "dry_run", "submit_documents", "batch_size")
			if any(self.has_value_changed(field) for field in locked):
				frappe.throw(_("Job settings cannot change while the job is running"))

	def on_trash(self) -> None:
		if self.status in RUNNING_STATES:
			frappe.throw(_("Cancel the job before deleting it"))
		for doctype in ("DataBridge Job Record", "DataBridge Execution Log"):
			frappe.db.delete(doctype, {"job": self.name})
