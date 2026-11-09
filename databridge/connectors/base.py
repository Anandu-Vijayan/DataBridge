"""Connector contract.

A connector reads one batch at a time from a cursor and never holds the whole dataset in memory.
The cursor is an opaque, JSON-serialisable dict that the runner stores as the job checkpoint.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConnectionConfig:
	"""Resolved connection settings. The password is excluded from repr so it cannot reach a log."""

	connector: str
	options: dict[str, Any] = field(default_factory=dict)
	password: str = field(default="", repr=False)

	def secrets(self) -> list[str]:
		return [self.password] if self.password else []


@dataclass
class SourceRow:
	"""One source record. error is set when the record could not even be parsed."""

	data: dict[str, Any]
	position: int  # 1-based record number in the source, for error messages
	error: str | None = None


@dataclass
class Batch:
	rows: list[SourceRow]
	cursor: dict[str, Any]  # where the next batch starts
	done: bool  # True when the source is exhausted


class Connector(ABC):
	name: str = ""

	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	@abstractmethod
	def test(self) -> None:
		"""Raise DataBridgeError(CONNECTION_FAILED / SOURCE_*) if the source is not usable."""

	@abstractmethod
	def list_objects(self) -> list[str]:
		"""Tables, sheets or logical names that can be migrated."""

	@abstractmethod
	def list_fields(self, source_object: str) -> list[str]:
		"""Column names of one source object."""

	@abstractmethod
	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		"""Read at most batch_size rows starting at cursor (None = start)."""

	def default_id_columns(self, source_object: str) -> list[str]:
		"""Columns that identify a record when the mapping does not name one. Files have none."""
		return []

	def preview(self, source_object: str, limit: int = 10) -> list[dict[str, Any]]:
		limit = max(1, min(int(limit), 50))
		batch = self.read_batch(source_object, None, limit)
		return [to_jsonable(row.data) for row in batch.rows if row.error is None]


def to_jsonable(value: Any) -> Any:
	"""Make any source value safe for JSON staging."""
	if isinstance(value, dict):
		return {str(k): to_jsonable(v) for k, v in value.items()}
	if isinstance(value, list | tuple):
		return [to_jsonable(v) for v in value]
	if isinstance(value, dt.datetime | dt.date | dt.time):
		return value.isoformat()
	if isinstance(value, decimal.Decimal):
		return str(value)
	if isinstance(value, bytes | bytearray | memoryview):
		return "base64:" + base64.b64encode(bytes(value)).decode()
	if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
		return None
	return value


def unique_headers(raw: list[Any]) -> list[str]:
	"""Header names with blanks and duplicates made unique (col_3, name_2)."""
	seen: dict[str, int] = {}
	result: list[str] = []
	for index, item in enumerate(raw, start=1):
		name = "" if item is None else str(item).strip()
		if not name:
			name = f"col_{index}"
		count = seen.get(name, 0) + 1
		seen[name] = count
		result.append(name if count == 1 else f"{name}_{count}")
	return result
