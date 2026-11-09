"""JSON (one top-level array of objects) and JSONL (one object per line).

JSONL resumes by byte offset. JSON arrays are streamed element by element with bounded memory, but a
resume re-parses (without keeping) the elements before the cursor. Use JSONL for very large files.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, TextIO

from databridge.connectors.base import Batch, Connector, SourceRow
from databridge.connectors.csv_connector import require_path
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

MAX_ELEMENT_CHARS = 50 * 1024 * 1024
CHUNK_CHARS = 1024 * 1024
_WS = " \t\r\n"


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
	"""Nested objects become dotted keys: {"a": {"b": 1}} -> {"a.b": 1}. Lists are kept as values."""
	flat: dict[str, Any] = {}
	for key, value in data.items():
		name = f"{prefix}{key}"
		if isinstance(value, dict):
			flat.update(flatten(value, f"{name}."))
		else:
			flat[name] = value
	return flat


def iter_json_array(handle: TextIO) -> Iterator[Any]:
	"""Yield the elements of a top-level JSON array without loading the whole document."""
	decoder = json.JSONDecoder()
	buf = ""
	pos = 0
	eof = False

	def more() -> bool:
		nonlocal buf, pos, eof
		data = handle.read(CHUNK_CHARS)
		if not data:
			eof = True
			return False
		buf = buf[pos:] + data
		pos = 0
		return True

	def next_char() -> str:
		nonlocal pos
		while True:
			while pos < len(buf) and buf[pos] in _WS:
				pos += 1
			if pos < len(buf):
				return buf[pos]
			if not more():
				return ""

	def bad(message: str) -> DataBridgeError:
		return DataBridgeError(errors.SOURCE_READ_ERROR, message)

	if next_char() != "[":
		raise bad("The JSON file must contain one top-level array of objects")
	pos += 1
	first = True
	while True:
		char = next_char()
		if char == "]" and first:
			return
		if char == "":
			raise bad("Unexpected end of JSON file")
		if not first:
			if char == "]":
				return
			if char != ",":
				raise bad("Expected a comma between JSON records")
			pos += 1
			char = next_char()
			if char in ("", "]"):
				raise bad("Trailing comma in JSON array")
		first = False
		while True:
			try:
				value, end = decoder.raw_decode(buf, pos)
			except json.JSONDecodeError as exc:
				if eof or not more():
					raise bad(f"Invalid JSON near character {exc.pos}") from None
				if len(buf) > MAX_ELEMENT_CHARS:
					raise bad("A JSON record is larger than 50 MB") from None
				continue
			if end == len(buf) and not eof and buf[pos] not in '{["' and more():
				continue  # a bare number or literal may have been cut by the chunk boundary
			pos = end
			break
		yield value


def _row_from_value(value: Any, position: int) -> SourceRow:
	if isinstance(value, dict):
		return SourceRow(flatten(value), position)
	return SourceRow({}, position, error="Record is not a JSON object")


def _discover_fields(rows: list[SourceRow]) -> list[str]:
	fields: dict[str, None] = {}
	for row in rows:
		if row.error is None:
			fields.update(dict.fromkeys(row.data))
	return list(fields)


def _encoding(connector: Connector) -> str:
	encoding = (connector.config.options.get("encoding") or "utf-8").lower()
	return "utf-8-sig" if encoding in ("utf-8", "utf8") else encoding


class JsonConnector(Connector):
	name = "JSON"

	def _open(self) -> TextIO:
		return open(require_path(self.config), encoding=_encoding(self), newline="")

	def test(self) -> None:
		with self._open() as handle:
			next(iter_json_array(handle), None)

	def list_objects(self) -> list[str]:
		return ["default"]

	def list_fields(self, source_object: str) -> list[str]:
		return _discover_fields(self.read_batch(source_object, None, 50).rows)

	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		skip = int(cursor["row"]) if cursor else 0
		rows: list[SourceRow] = []
		position = 0
		done = True
		with self._open() as handle:
			for position, value in enumerate(iter_json_array(handle), start=1):
				if position <= skip:
					continue
				if len(rows) >= batch_size:
					done = False
					position -= 1
					break
				rows.append(_row_from_value(value, position))
		return Batch(rows=rows, cursor={"row": skip + len(rows)}, done=done)


class JsonlConnector(Connector):
	name = "JSONL"

	def _open(self):
		return open(require_path(self.config), "rb")

	def test(self) -> None:
		self.read_batch("default", None, 1)

	def list_objects(self) -> list[str]:
		return ["default"]

	def list_fields(self, source_object: str) -> list[str]:
		return _discover_fields(self.read_batch(source_object, None, 50).rows)

	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		offset = int(cursor["offset"]) if cursor else 0
		row_no = int(cursor["row"]) if cursor else 0
		encoding = _encoding(self)
		rows: list[SourceRow] = []
		with self._open() as handle:
			handle.seek(offset)
			done = False
			while len(rows) < batch_size:
				line = handle.readline()
				if not line:
					done = True
					break
				offset += len(line)
				if offset == len(line) and encoding == "utf-8-sig" and line.startswith(b"\xef\xbb\xbf"):
					line = line[3:]
				if not line.strip():
					continue
				row_no += 1
				try:
					value = json.loads(line.decode(encoding.replace("-sig", "")))
				except (UnicodeDecodeError, json.JSONDecodeError):
					rows.append(SourceRow({}, row_no, error="Line is not valid JSON"))
					continue
				rows.append(_row_from_value(value, row_no))
			if not done:
				done = handle.peek(1) == b""
		return Batch(rows=rows, cursor={"offset": offset, "row": row_no}, done=done)
