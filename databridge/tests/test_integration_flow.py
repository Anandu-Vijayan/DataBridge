"""End-to-end tests against a real site. Run with:

    bench --site <site> run-tests --app databridge

The queue is replaced by a loop that calls run_batch() directly, so each batch is still its own
job: lock, process one batch, commit, ask for the next.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe

from databridge.api import connection as connection_api
from databridge.api import run as run_api
from databridge.engine.frappe_store import run_batch
from databridge.tests.base import DataBridgeTestCase

JOB = "DataBridge Job"
REFERENCE = "DataBridge Migration Reference"
PREFIX = "DBTEST "
CSV = (
	"id,desc,prio,due\n"
	f"1,{PREFIX}one,High,02/01/2026\n"
	f"2,{PREFIX}two,Low,03/01/2026\n"
	f"3,{PREFIX}three,Medium,04/01/2026\n"
)


class TestDataBridgeFlow(DataBridgeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.tag = frappe.generate_hash(length=6)
		self.created: list[tuple[str, str]] = []
		for target in ("databridge.api.run.enqueue_batch", "databridge.engine.frappe_store.enqueue_batch"):
			patcher = patch(target)
			patcher.start()
			self.addCleanup(patcher.stop)
		self.addCleanup(self.cleanup)

	# ------------------------------------------------------------------ helpers
	def cleanup(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self.created):
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", f"{PREFIX}%")})
		frappe.db.delete(REFERENCE, {"connection": ("like", f"dbt-{self.tag}%")})
		frappe.db.commit()

	def track(self, doc):
		self.created.append((doc.doctype, doc.name))
		return doc

	def make_connection(self, text: str = CSV):
		file_doc = frappe.get_doc(
			{"doctype": "File", "file_name": f"dbt-{self.tag}.csv", "content": text, "is_private": 1}
		).insert(ignore_permissions=True)
		self.created.append(("File", file_doc.name))
		return self.track(
			frappe.get_doc(
				{
					"doctype": "DataBridge Connection",
					"connection_name": f"dbt-{self.tag}",
					"connector": "CSV",
					"source_file": file_doc.file_url,
				}
			).insert()
		)

	def make_mapping(self, connection, role_column: bool = False):
		fields = [
			{"source_field": "desc", "target_field": "description"},
			{"source_field": "prio", "target_field": "priority"},
			{
				"source_field": "due",
				"target_field": "date",
				"transform": "date",
				"transform_args": "%d/%m/%Y",
			},
		]
		if role_column:
			fields.append({"source_field": "role", "target_field": "role"})
		return self.track(
			frappe.get_doc(
				{
					"doctype": "DataBridge Mapping",
					"mapping_name": f"dbt-map-{self.tag}",
					"connection": connection.name,
					"source_object": "default",
					"target_doctype": "ToDo",
					"source_id_field": "id",
					"default_mode": "Insert",
					"fields": fields,
				}
			).insert()
		)

	def make_job(self, mapping, batch_size: int = 2):
		return self.track(
			frappe.get_doc(
				{"doctype": JOB, "mapping": mapping.name, "mode": "Insert", "batch_size": batch_size}
			).insert()
		)

	def drain(self, job_name: str) -> str:
		try:
			for _ in range(60):
				status = frappe.db.get_value(JOB, job_name, "status")
				if status not in ("Queued", "Running"):
					return status
				run_batch(job_name)
			raise AssertionError("job did not finish")
		finally:
			frappe.set_user("Administrator")

	def start(self, job, **kwargs):
		result = run_api.start_job(job.name, **kwargs)
		self.assertTrue(result["ok"], result)
		return result

	def todos(self):
		return frappe.get_all("ToDo", filters={"description": ("like", f"{PREFIX}%")}, pluck="name")

	def refs(self, connection):
		return frappe.db.count(REFERENCE, {"connection": connection.name})

	# ------------------------------------------------------------------ tests
	def test_insert_run_writes_documents_and_references(self):
		conn = self.make_connection()
		job = self.make_job(self.make_mapping(conn))
		self.start(job)
		self.assertEqual(self.drain(job.name), "Completed")
		job.reload()
		self.assertEqual((job.total_read, job.inserted, job.failed), (3, 3, 0))
		self.assertEqual(len(self.todos()), 3)
		self.assertEqual(self.refs(conn), 3)
		self.assertEqual(frappe.db.get_value("ToDo", self.todos()[0], "docstatus"), 0)

	def test_running_the_same_source_twice_creates_no_duplicates(self):
		conn = self.make_connection()
		mapping = self.make_mapping(conn)
		first = self.make_job(mapping)
		self.start(first)
		self.drain(first.name)
		second = self.make_job(mapping)
		self.start(second)
		self.assertEqual(self.drain(second.name), "Completed")
		second.reload()
		self.assertEqual((second.inserted, second.skipped), (0, 3))
		self.assertEqual(len(self.todos()), 3)
		self.assertEqual(self.refs(conn), 3)

	def test_dry_run_validates_and_writes_nothing(self):
		conn = self.make_connection()
		job = self.make_job(self.make_mapping(conn))
		self.start(job, dry_run=1)
		self.assertEqual(self.drain(job.name), "Completed")
		job.reload()
		self.assertEqual(job.validated, 3)
		self.assertEqual(self.todos(), [])
		self.assertEqual(self.refs(conn), 0)

	def test_failed_records_are_kept_and_can_be_retried(self):
		role = f"DBT Role {self.tag}"
		text = f"id,desc,prio,due,role\n1,{PREFIX}one,High,02/01/2026,\n2,{PREFIX}two,Low,03/01/2026,{role}\n"
		conn = self.make_connection(text)
		job = self.make_job(self.make_mapping(conn, role_column=True))
		self.start(job)
		self.assertEqual(self.drain(job.name), "Completed With Errors")
		job.reload()
		self.assertEqual((job.inserted, job.failed), (1, 1))
		failed = frappe.get_all(
			"DataBridge Job Record",
			filters={"job": job.name, "status": "Failed"},
			fields=["error_code", "source_id"],
		)
		self.assertEqual([(f.source_id, f.error_code) for f in failed], [("2", "LINK_NOT_FOUND")])

		self.track(
			frappe.get_doc({"doctype": "Role", "role_name": role}).insert()
		)  # the data problem is fixed
		result = run_api.retry_failed(job.name)
		self.assertTrue(result["ok"], result)
		self.assertEqual(self.drain(job.name), "Completed")
		job.reload()
		self.assertEqual((job.failed, job.inserted, job.total_read), (0, 2, 2))
		self.assertEqual(len(self.todos()), 2)

	def test_pause_and_resume_continue_from_checkpoint(self):
		conn = self.make_connection()
		job = self.make_job(self.make_mapping(conn), batch_size=1)
		self.start(job)
		run_batch(job.name)
		frappe.set_user("Administrator")
		self.assertEqual(len(self.todos()), 1)
		self.assertTrue(run_api.pause_job(job.name)["ok"])
		run_batch(job.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.db.get_value(JOB, job.name, "status"), "Paused")
		self.assertTrue(run_api.resume_job(job.name)["ok"])
		self.assertEqual(self.drain(job.name), "Completed")
		self.assertEqual(len(self.todos()), 3)
		self.assertEqual(self.refs(conn), 3)

	def test_running_job_ignores_later_mapping_edits(self):
		conn = self.make_connection()
		mapping = self.make_mapping(conn)
		job = self.make_job(mapping, batch_size=1)
		self.start(job)
		run_batch(job.name)
		frappe.set_user("Administrator")
		mapping.reload()
		mapping.fields[0].transform = "upper"
		mapping.save()
		self.drain(job.name)
		descriptions = frappe.get_all(
			"ToDo", filters={"description": ("like", f"{PREFIX}%")}, pluck="description"
		)
		self.assertEqual(len(descriptions), 3)
		self.assertTrue(all("one" in d or "two" in d or "three" in d for d in descriptions), descriptions)

	def test_submit_flag_rejected_for_non_submittable_target(self):
		conn = self.make_connection()
		job = self.make_job(self.make_mapping(conn))
		job.submit_documents = 1
		job.save()
		result = run_api.start_job(job.name)
		self.assertFalse(result["ok"])
		self.assertEqual(result["error"]["code"], "SUBMIT_NOT_ALLOWED")

	def test_control_actions_follow_the_state_machine(self):
		conn = self.make_connection()
		job = self.make_job(self.make_mapping(conn))
		for action in (run_api.pause_job, run_api.resume_job, run_api.cancel_job, run_api.retry_failed):
			result = action(job.name)
			self.assertEqual(result["error"]["code"], "INVALID_STATE", action.__name__)

	def test_mapping_with_unknown_target_field_is_rejected(self):
		conn = self.make_connection()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "DataBridge Mapping",
					"mapping_name": f"dbt-bad-{self.tag}",
					"connection": conn.name,
					"source_object": "default",
					"target_doctype": "ToDo",
					"source_id_field": "id",
					"fields": [{"source_field": "desc", "target_field": "no_such_field"}],
				}
			).insert()

	def test_connection_errors_never_leak_the_password(self):
		secret = f"pw-{frappe.generate_hash(length=10)}"
		conn = self.track(
			frappe.get_doc(
				{
					"doctype": "DataBridge Connection",
					"connection_name": f"dbt-{self.tag}-db",
					"connector": "MySQL",
					"host": "127.0.0.1",
					"port": 1,
					"database": "nothing",
					"username": "nobody",
					"password": secret,
				}
			).insert()
		)
		result = connection_api.test_connection(conn.name)
		self.assertFalse(result["ok"])
		self.assertNotIn(secret, json.dumps(result))
		self.assertNotIn(secret, frappe.db.get_value("DataBridge Connection", conn.name, "last_error") or "")
		self.assertNotIn(
			secret, json.dumps(frappe.get_doc("DataBridge Connection", conn.name).as_dict(), default=str)
		)
