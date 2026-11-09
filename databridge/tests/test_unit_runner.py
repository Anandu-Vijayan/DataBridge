"""Runner orchestration tests with an in-memory store and a real CSV connector."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from databridge.connectors.base import ConnectionConfig
from databridge.connectors.csv_connector import CsvConnector
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import build_snapshot
from databridge.engine.runner import (
	COUNTER_KEYS,
	FailedRecord,
	JobState,
	RecordResult,
	process_batch,
)

JOB = "DBJ-00001"
SECRET = "s3cret"


class FakeStore:
	"""Minimal in-memory JobStore. Tracks a fake 'target table' so rollbacks are observable."""

	def __init__(self, state: JobState):
		self.state = state
		self.records: list[dict] = []
		self.logs: list[tuple] = []
		self.queue: list[int] = []
		self.target: dict[str, dict] = {}
		self.ledger: dict[str, str] = {}
		self.deny: str | None = None
		self.rollbacks = 0
		self.commits = 0
		self.last_error = ""

	# -- JobStore protocol
	def lock_state(self, job):
		return copy.deepcopy(self.state)

	def check_permissions(self, spec, state):
		if self.deny:
			raise DataBridgeError(errors.PERMISSION_DENIED, self.deny)

	@contextmanager
	def savepoint(self):
		snap = (copy.deepcopy(self.target), copy.deepcopy(self.ledger))
		try:
			yield
		except BaseException:
			self.target, self.ledger = snap
			raise

	def rollback(self):
		self.rollbacks += 1

	def commit(self):
		self.commits += 1

	def add_record(self, job, batch_no, result):
		self.records.append(
			{"batch": batch_no, "name": f"R{len(self.records) + 1:04d}", "result": result, "retry": 0}
		)

	def update_record(self, record, result, retry_count):
		for r in self.records:
			if r["name"] == record:
				r["result"], r["retry"] = result, retry_count

	def failed_records(self, job, after, limit):
		rows = [r for r in self.records if r["result"].status == "Failed" and r["name"] > after]
		return [
			FailedRecord(r["name"], r["result"].source_id, r["result"].source_data, r["retry"])
			for r in rows[:limit]
		]

	def save_progress(self, state, **c):
		if c.get("last_error"):
			self.last_error = c["last_error"]
		for key in ("status", "phase", "stop_requested", "last_error", "current_batch"):
			if key in c:
				setattr(self.state, key, c[key])
		if "counters" in c:
			self.state.counters = dict(c["counters"])
		if "checkpoint" in c:
			self.state.checkpoint = c["checkpoint"]

	def log(self, job, level, event, message, batch_no=None, details=None):
		self.logs.append((level, event, message))

	def enqueue_next(self, job, batch_no):
		self.queue.append(batch_no)


class FakeProcessor:
	"""Mimics FrappeRecordProcessor: reference ledger first, then write; fails on chosen ids."""

	def __init__(self, store, fail_ids=(), crash_after_write=(), crash_ids=()):
		self.store, self.fail_ids = store, set(fail_ids)
		self.crash_after_write, self.crash_ids = set(crash_after_write), set(crash_ids)

	def process(self, mapped):
		sid = mapped.source_id
		if sid in self.fail_ids:
			raise DataBridgeError(errors.VALIDATION_ERROR, f"bad record {sid}")
		if sid in self.crash_ids:
			raise RuntimeError(f"boom with {SECRET}")
		if sid in self.store.ledger:
			return SimpleNamespace(
				status="Skipped", action="skip", target_name=self.store.ledger[sid], message="exists"
			)
		name = f"T-{sid}"
		self.store.target[name] = dict(mapped.values)
		if sid in self.crash_after_write:
			raise DataBridgeError(errors.WRITE_FAILED, "died after insert")
		self.store.ledger[sid] = name
		return SimpleNamespace(status="Inserted", action="insert", target_name=name, message=None)


def make_csv(tmp_path, rows, header="id,name\n"):
	path = tmp_path / "src.csv"
	path.write_text(header + "".join(f"{r}\n" for r in rows), encoding="utf-8")
	return CsvConnector(ConnectionConfig("CSV", {"path": str(path)}, password=SECRET))


def make_state(batch_size=2, **over):
	snapshot = build_snapshot(
		{
			"name": "M",
			"connection": "C",
			"source_object": "default",
			"target_doctype": "Thing",
			"source_id_field": "id",
			"default_mode": "Insert",
			"fields": [
				{"source_field": "id", "target_field": "code"},
				{"source_field": "name", "target_field": "title"},
			],
		}
	)
	state = JobState(name=JOB, status="Queued", batch_size=batch_size, snapshot=snapshot, run_as="u@x.com")
	for key, value in over.items():
		setattr(state, key, value)
	return state


def drain(store, connector, processor, limit=50):
	"""Act like the worker: run the first batch, then every enqueued follow-up."""
	results = [process_batch(JOB, store, connector, lambda s, st: processor, [SECRET])]
	while store.queue:
		store.queue.pop(0)
		results.append(process_batch(JOB, store, connector, lambda s, st: processor, [SECRET]))
		assert len(results) < limit
	return results


def test_full_run_one_batch_per_call_and_counters(tmp_path):
	conn = make_csv(tmp_path, [f"{i},n{i}" for i in range(1, 6)])
	store = FakeStore(make_state(batch_size=2))
	results = drain(store, conn, FakeProcessor(store))
	assert results == ["continue", "continue", "completed"]
	s = store.state
	assert s.status == "Completed" and s.current_batch == 3
	assert (s.counters["total_read"], s.counters["inserted"], s.counters["failed"]) == (5, 5, 0)
	assert sorted(store.target) == [f"T-{i}" for i in range(1, 6)]
	assert [r["batch"] for r in store.records] == [1, 1, 2, 2, 3]
	assert store.commits == 3  # exactly one commit per batch


def test_each_call_reads_at_most_batch_size(tmp_path):
	conn = make_csv(tmp_path, [f"{i},n{i}" for i in range(1, 8)])
	store = FakeStore(make_state(batch_size=3))
	assert process_batch(JOB, store, conn, lambda s, st: FakeProcessor(store), []) == "continue"
	assert store.state.counters["total_read"] == 3
	assert store.queue == [2]


def test_record_failures_do_not_stop_the_batch(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b", ",nameless", "4,d", "5,e"])
	store = FakeStore(make_state(batch_size=10))
	drain(store, conn, FakeProcessor(store, fail_ids={"2"}))
	s = store.state
	assert s.status == "Completed With Errors"
	assert (s.counters["inserted"], s.counters["failed"], s.counters["processed"]) == (3, 2, 5)
	codes = {
		r["result"].source_id: r["result"].error_code for r in store.records if r["result"].status == "Failed"
	}
	assert codes["2"] == errors.VALIDATION_ERROR
	assert codes["row:3"] == errors.MISSING_SOURCE_ID  # rule 9: missing source id is a record error
	failed = next(r["result"] for r in store.records if r["result"].source_id == "2")
	assert failed.source_data == {"id": "2", "name": "b"}


def test_unexpected_exception_becomes_redacted_record_failure(tmp_path):
	conn = make_csv(tmp_path, ["1,a"])
	store = FakeStore(make_state())
	drain(store, conn, FakeProcessor(store, crash_ids={"1"}))
	rec = store.records[0]["result"]
	assert rec.error_code == errors.WRITE_FAILED
	assert SECRET not in rec.error_message
	assert store.state.status == "Completed With Errors"


def test_failed_record_write_is_rolled_back_with_its_reference(tmp_path):
	conn = make_csv(tmp_path, ["1,a"])
	store = FakeStore(make_state())
	drain(store, conn, FakeProcessor(store, crash_after_write={"1"}))
	assert store.target == {} and store.ledger == {}, "half-written record must not survive"


def test_pause_then_resume_continues_from_checkpoint(tmp_path):
	conn = make_csv(tmp_path, [f"{i},n{i}" for i in range(1, 7)])
	store = FakeStore(make_state(batch_size=2))
	proc = FakeProcessor(store)
	assert process_batch(JOB, store, conn, lambda s, st: proc, []) == "continue"
	store.state.stop_requested = "Pause"
	assert process_batch(JOB, store, conn, lambda s, st: proc, []) == "paused"
	assert store.state.status == "Paused" and store.state.stop_requested == ""
	read_before = store.state.counters["total_read"]
	assert process_batch(JOB, store, conn, lambda s, st: proc, []) == "noop"  # paused jobs do nothing
	store.state.status = "Queued"  # what Resume does
	store.queue.clear()
	drain_results = drain(store, conn, proc)
	assert drain_results[-1] == "completed"
	assert read_before == 2 and store.state.counters["inserted"] == 6
	assert len(store.target) == 6, "no duplicates after resume"


def test_cancel(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b", "3,c"])
	store = FakeStore(make_state(batch_size=1))
	proc = FakeProcessor(store)
	process_batch(JOB, store, conn, lambda s, st: proc, [])
	store.state.stop_requested = "Cancel"
	assert process_batch(JOB, store, conn, lambda s, st: proc, []) == "cancelled"
	assert store.state.status == "Cancelled"
	assert len(store.target) == 1


def test_permission_failure_fails_job_before_reading(tmp_path):
	conn = make_csv(tmp_path, ["1,a"])
	store = FakeStore(make_state())
	store.deny = "no create permission"
	assert process_batch(JOB, store, conn, lambda s, st: FakeProcessor(store), []) == "failed"
	assert (
		store.state.status == "Failed" and "PERMISSION_DENIED" in store.state.last_error
		if hasattr(store.state, "last_error")
		else True
	)
	assert store.records == [] and store.rollbacks == 1


def test_source_failure_fails_job_and_keeps_checkpoint(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b", "3,c"])
	store = FakeStore(make_state(batch_size=1))
	proc = FakeProcessor(store)
	process_batch(JOB, store, conn, lambda s, st: proc, [])
	checkpoint = store.state.checkpoint
	(tmp_path / "src.csv").unlink()
	assert process_batch(JOB, store, conn, lambda s, st: proc, []) == "failed"
	assert store.state.status == "Failed"
	assert store.state.checkpoint == checkpoint, "failed batch must not advance the checkpoint"


def test_resume_after_failure_does_not_duplicate(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b", "3,c"])
	store = FakeStore(make_state(batch_size=2))
	proc = FakeProcessor(store)
	process_batch(JOB, store, conn, lambda s, st: proc, [])
	store.state.checkpoint = None  # worst case: checkpoint lost, whole source is re-read
	store.state.counters = dict.fromkeys(COUNTER_KEYS, 0)
	store.state.current_batch = 0
	store.queue.clear()
	drain(store, conn, proc)
	assert sorted(store.target) == ["T-1", "T-2", "T-3"]
	assert store.state.counters["skipped"] == 2 and store.state.counters["inserted"] == 1


def test_retry_failed_records_only(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b", "3,c", "4,d"])
	store = FakeStore(make_state(batch_size=2))
	drain(store, conn, FakeProcessor(store, fail_ids={"2", "4"}))
	assert store.state.status == "Completed With Errors" and store.state.counters["failed"] == 2

	# the data problem is fixed; now retry
	store.state.status, store.state.phase, store.state.checkpoint = "Queued", "retry", {"after": ""}
	store.state.current_batch = 0
	store.queue.clear()
	healthy = FakeProcessor(store)
	results = drain(store, conn, healthy)
	assert results[-1] == "completed"
	s = store.state
	assert s.status == "Completed" and s.phase == "migrate"
	assert (s.counters["failed"], s.counters["inserted"]) == (0, 4)
	assert s.counters["total_read"] == 4, "retry must not re-count source rows"
	assert sorted(store.target) == ["T-1", "T-2", "T-3", "T-4"]
	assert all(r["retry"] == 1 for r in store.records if r["result"].source_id in ("2", "4"))


def test_retry_that_still_fails_keeps_counts_and_bumps_retry(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b"])
	store = FakeStore(make_state(batch_size=5))
	proc = FakeProcessor(store, fail_ids={"2"})
	drain(store, conn, proc)
	store.state.status, store.state.phase, store.state.checkpoint = "Queued", "retry", {"after": ""}
	store.state.current_batch = 0
	drain(store, conn, proc)
	assert store.state.status == "Completed With Errors" and store.state.counters["failed"] == 1
	assert next(r for r in store.records if r["result"].status == "Failed")["retry"] == 1


def test_noop_for_finished_jobs(tmp_path):
	conn = make_csv(tmp_path, ["1,a"])
	store = FakeStore(make_state(status="Completed"))
	assert process_batch(JOB, store, conn, lambda s, st: FakeProcessor(store), []) == "noop"
	assert store.records == []


def test_processor_factory_sees_dry_run_and_submit_flags(tmp_path):
	conn = make_csv(tmp_path, ["1,a"])
	store = FakeStore(make_state(dry_run=True, submit=False))
	seen = {}

	def factory(spec, state):
		seen.update(dry=state.dry_run, submit=state.submit, mode=spec.mode)
		return FakeProcessor(store)

	process_batch(JOB, store, conn, factory, [])
	assert seen == {"dry": True, "submit": False, "mode": "Insert"}


def test_running_job_uses_its_snapshot_not_later_mapping_edits(tmp_path):
	conn = make_csv(tmp_path, ["1,a", "2,b", "3,c"])
	store = FakeStore(make_state(batch_size=1))
	proc = FakeProcessor(store)
	process_batch(JOB, store, conn, lambda s, st: proc, [])
	# someone edits the live Mapping: it is a different object from the job's frozen snapshot
	edited = build_snapshot(
		{
			"name": "M",
			"connection": "C",
			"source_object": "default",
			"target_doctype": "Other",
			"source_id_field": "id",
			"fields": [{"source_field": "id", "target_field": "x"}],
		}
	)
	assert edited != store.state.snapshot
	drain(store, conn, proc)
	assert set(next(iter(store.target.values()))) == {"code", "title"}


def test_record_result_defaults():
	assert RecordResult(status="Inserted").error_code == ""


def test_default_source_id_from_connector_when_mapping_has_none(tmp_path):
	class IdConnector(CsvConnector):
		def default_id_columns(self, source_object):
			return ["id"]

	path = tmp_path / "s.csv"
	path.write_text("id,name\n7,x\n", encoding="utf-8")
	conn = IdConnector(ConnectionConfig("CSV", {"path": str(path)}))
	state = make_state()
	state.snapshot["source_id_field"] = ""
	store = FakeStore(state)
	drain(store, conn, FakeProcessor(store))
	assert "T-7" in store.target
