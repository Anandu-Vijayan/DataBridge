"""Batch orchestration. One call processes ONE batch, commits, then asks the store to enqueue the next.

Everything Frappe-specific (row locks, DB writes, the queue) sits behind the JobStore protocol and
the processor object, so this module is plain Python and fully unit-tested without a site.
"""

from __future__ import annotations

from collections.abc import Callable, ContextManager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from databridge.connectors.base import Connector, SourceRow, to_jsonable
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import (
	MappedRecord,
	MappingSpec,
	extract_source_id,
	map_record,
	spec_from_snapshot,
)
from databridge.utils.redact import safe_error_message

QUEUED = "Queued"
RUNNING = "Running"
PAUSED = "Paused"
CANCELLED = "Cancelled"
COMPLETED = "Completed"
COMPLETED_WITH_ERRORS = "Completed With Errors"
FAILED = "Failed"
ACTIVE_STATES = (QUEUED, RUNNING)

COUNTER_KEYS = ("total_read", "processed", "inserted", "updated", "skipped", "failed", "validated")
_STATUS_COUNTER = {
	"Inserted": "inserted",
	"Updated": "updated",
	"Skipped": "skipped",
	"Validated": "validated",
	"Failed": "failed",
}


@dataclass
class JobState:
	name: str
	status: str
	stop_requested: str = ""
	phase: str = "migrate"
	dry_run: bool = False
	submit: bool = False
	batch_size: int = 200
	snapshot: dict[str, Any] = field(default_factory=dict)
	checkpoint: dict[str, Any] | None = None
	counters: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTER_KEYS, 0))
	current_batch: int = 0
	run_as: str = ""


@dataclass
class FailedRecord:
	name: str
	source_id: str
	source_data: dict[str, Any] | None
	retry_count: int = 0


@dataclass
class RecordResult:
	status: str
	action: str = ""
	source_id: str = ""
	target_name: str | None = None
	error_code: str = ""
	error_message: str = ""
	mapped: dict[str, Any] | None = None
	source_data: dict[str, Any] | None = None


class Processor(Protocol):
	def process(
		self, mapped: MappedRecord
	) -> Any:  # returns an object with status/action/target_name/message
		...


class JobStore(Protocol):
	def lock_state(self, job: str) -> JobState: ...
	def check_permissions(self, spec: MappingSpec, state: JobState) -> None: ...
	def savepoint(self) -> ContextManager[None]: ...
	def rollback(self) -> None: ...
	def commit(self) -> None: ...
	def add_record(self, job: str, batch_no: int, result: RecordResult) -> None: ...
	def update_record(self, record: str, result: RecordResult, retry_count: int) -> None: ...
	def failed_records(self, job: str, after: str, limit: int) -> list[FailedRecord]: ...
	def save_progress(self, state: JobState, **changes: Any) -> None: ...
	def log(
		self,
		job: str,
		level: str,
		event: str,
		message: str,
		batch_no: int | None = None,
		details: dict | None = None,
	) -> None: ...
	def enqueue_next(self, job: str, batch_no: int) -> None: ...


ProcessorFactory = Callable[[MappingSpec, JobState], Processor]


def _best_effort_source_id(row: SourceRow, source_id_field: str) -> str:
	try:
		return extract_source_id(row.data, source_id_field)
	except DataBridgeError:
		return f"row:{row.position}"


def handle_row(
	row: SourceRow, spec: MappingSpec, processor: Processor, store: JobStore, secrets: list[str]
) -> RecordResult:
	"""Map and write one row. Never raises: every problem becomes a Failed RecordResult."""
	source_id = ""
	try:
		if row.error:
			raise DataBridgeError(errors.SOURCE_READ_ERROR, row.error)
		mapped = map_record(row.data, spec)
		source_id = mapped.source_id
		with store.savepoint():
			outcome = processor.process(mapped)
		return RecordResult(
			status=outcome.status,
			action=outcome.action,
			source_id=source_id,
			target_name=outcome.target_name,
			error_message=(outcome.message or "") if outcome.status == "Skipped" else "",
		)
	except DataBridgeError as exc:
		code, message = exc.code, exc.message
	except Exception as exc:  # a record must never take the batch down with it
		code, message = errors.WRITE_FAILED, safe_error_message(exc, secrets)
	return RecordResult(
		status="Failed",
		action="fail",
		source_id=source_id or _best_effort_source_id(row, spec.source_id_field),
		error_code=code,
		error_message=message[:1000],
		source_data=to_jsonable(row.data) if row.data else None,
	)


def _apply(counters: dict[str, int], result: RecordResult) -> None:
	counters["processed"] += 1
	counters[_STATUS_COUNTER[result.status]] += 1


def _effective_spec(spec: MappingSpec, connector: Connector) -> MappingSpec:
	if spec.source_id_field:
		return spec
	return replace(spec, source_id_field=",".join(connector.default_id_columns(spec.source_object)))


def fail_job(store: JobStore, state: JobState, exc: BaseException, secrets: list[str]) -> str:
	store.rollback()
	message = exc.message if isinstance(exc, DataBridgeError) else safe_error_message(exc, secrets)
	code = exc.code if isinstance(exc, DataBridgeError) else errors.JOB_FAILED
	store.save_progress(state, status=FAILED, last_error=f"[{code}] {message}"[:1000], ended=True)
	store.log(state.name, "Error", "job_failed", message[:1000], state.current_batch + 1, {"code": code})
	store.commit()
	return "failed"


def _migrate_batch(
	state: JobState,
	spec: MappingSpec,
	connector: Connector,
	processor: Processor,
	store: JobStore,
	counters: dict[str, int],
	batch_no: int,
	secrets: list[str],
) -> tuple[bool, dict[str, Any]]:
	spec = _effective_spec(spec, connector)
	batch = connector.read_batch(spec.source_object, state.checkpoint, state.batch_size)
	for row in batch.rows:
		counters["total_read"] += 1
		result = handle_row(row, spec, processor, store, secrets)
		store.add_record(state.name, batch_no, result)
		_apply(counters, result)
	return batch.done, batch.cursor


def _retry_batch(
	state: JobState,
	spec: MappingSpec,
	connector: Connector,
	processor: Processor,
	store: JobStore,
	counters: dict[str, int],
	batch_no: int,
	secrets: list[str],
) -> tuple[bool, dict[str, Any]]:
	spec = _effective_spec(spec, connector)
	after = (state.checkpoint or {}).get("after", "")
	records = store.failed_records(state.name, after, state.batch_size)
	for record in records:
		if record.source_data:
			row = SourceRow(record.source_data, 0)
			result = handle_row(row, spec, processor, store, secrets)
			result.source_id = record.source_id
		else:
			result = RecordResult(
				status="Failed",
				action="fail",
				source_id=record.source_id,
				error_code=errors.SOURCE_READ_ERROR,
				error_message="The source row was not readable, fix the source and run a new job",
			)
		store.update_record(record.name, result, record.retry_count + 1)
		if result.status != "Failed":
			counters["failed"] -= 1
			counters[_STATUS_COUNTER[result.status]] += 1
	cursor = {"after": records[-1].name} if records else {"after": after}
	return len(records) < state.batch_size, cursor


def process_batch(
	job_name: str,
	store: JobStore,
	connector: Connector,
	processor_factory: ProcessorFactory,
	secrets: list[str] | None = None,
) -> str:
	"""Process one batch. Returns noop | cancelled | paused | continue | completed | failed."""
	secrets = secrets or []
	state = store.lock_state(job_name)
	if state.status not in ACTIVE_STATES:
		return "noop"
	if state.stop_requested == "Cancel":
		store.save_progress(state, status=CANCELLED, stop_requested="", ended=True)
		store.log(job_name, "Warning", "job_cancelled", "Cancelled by user", state.current_batch)
		store.commit()
		return "cancelled"
	if state.stop_requested == "Pause":
		store.save_progress(state, status=PAUSED, stop_requested="")
		store.log(job_name, "Info", "job_paused", "Paused by user", state.current_batch)
		store.commit()
		return "paused"

	try:
		spec = spec_from_snapshot(state.snapshot)
		store.check_permissions(spec, state)
	except DataBridgeError as exc:
		return fail_job(store, state, exc, secrets)

	batch_no = state.current_batch + 1
	if batch_no == 1 and state.phase == "migrate":
		store.save_progress(state, status=RUNNING, started=True)
		store.log(job_name, "Info", "job_started", f"Mode {spec.mode}, dry run {state.dry_run}", 1)
	else:
		store.save_progress(state, status=RUNNING)

	counters = dict(state.counters)
	try:
		processor = processor_factory(spec, state)
		run = _retry_batch if state.phase == "retry" else _migrate_batch
		done, checkpoint = run(state, spec, connector, processor, store, counters, batch_no, secrets)
	except Exception as exc:
		return fail_job(store, state, exc, secrets)

	changes: dict[str, Any] = {"counters": counters, "checkpoint": checkpoint, "current_batch": batch_no}
	store.log(
		job_name,
		"Info",
		"batch_done",
		f"Batch {batch_no} finished",
		batch_no,
		{k: counters[k] for k in COUNTER_KEYS},
	)
	if done:
		final = COMPLETED_WITH_ERRORS if counters["failed"] > 0 else COMPLETED
		store.save_progress(state, status=final, phase="migrate", ended=True, last_error="", **changes)
		store.log(
			job_name,
			"Info",
			"job_completed",
			f"Finished: {final}",
			batch_no,
			{k: counters[k] for k in COUNTER_KEYS},
		)
		store.commit()
		return "completed"
	store.save_progress(state, status=RUNNING, **changes)
	store.commit()
	store.enqueue_next(job_name, batch_no + 1)
	return "continue"
