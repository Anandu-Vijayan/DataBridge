"""CSV and TSV. Streams by byte offset, so any batch can start without re-reading the file."""

from __future__ import annotations

import csv
import os
from typing import Any, BinaryIO

from databridge.connectors.base import Batch, ConnectionConfig, Connector, SourceRow, unique_headers
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

MAX_RECORD_BYTES = 10 * 1024 * 1024  # guards against an unbalanced quote swallowing the file
_BANNED_ENCODINGS = ("utf-16", "utf-32", "utf_16", "utf_32")


def require_path(config: ConnectionConfig) -> str:
	path = config.options.get("path")
	if not path or not os.path.isfile(path):
		raise DataBridgeError(errors.SOURCE_FILE_MISSING, "The source file is missing or unreadable")
	return path


class CsvConnector(Connector):
	name = "CSV"
	default_delimiter = ","

	def _delimiter(self) -> str:
		delimiter = self.config.options.get("delimiter") or self.default_delimiter
		if delimiter == "\\t":
			delimiter = "\t"
		if len(delimiter) != 1:
			raise DataBridgeError(errors.INVALID_INPUT, "Delimiter must be a single character")
		return delimiter

	def _encoding(self) -> str:
		encoding = (self.config.options.get("encoding") or "utf-8").lower()
		if encoding.startswith(_BANNED_ENCODINGS):
			raise DataBridgeError(
				errors.INVALID_INPUT, "UTF-16 and UTF-32 files are not supported, use UTF-8"
			)
		return encoding

	def _has_header(self) -> bool:
		return bool(int(self.config.options.get("has_header", 1)))

	@staticmethod
	def _read_raw(handle: BinaryIO) -> tuple[bytes | None, int]:
		"""Read one logical record (may span lines inside quotes). Returns (raw bytes, bytes consumed)."""
		consumed = 0
		chunks: list[bytes] = []
		quotes = 0
		while True:
			line = handle.readline()
			if not line:
				break
			consumed += len(line)
			if consumed > MAX_RECORD_BYTES:
				raise DataBridgeError(
					errors.SOURCE_READ_ERROR, "A record is larger than 10 MB, check the quoting"
				)
			chunks.append(line)
			quotes += line.count(b'"')
			if quotes % 2 == 0:
				break
		return (b"".join(chunks) if chunks else None), consumed

	def _parse(self, raw: bytes) -> list[str]:
		text = raw.decode(self._encoding(), errors="strict")
		if text.startswith("﻿"):
			text = text[1:]
		return next(csv.reader([text], delimiter=self._delimiter()), [])

	def _open(self) -> BinaryIO:
		# Path comes from require_path(): a private File resolved by resolve_private_file_path().
		return open(require_path(self.config), "rb")  # nosemgrep

	def _header_and_start(self, handle: BinaryIO) -> tuple[list[str], int]:
		raw, consumed = self._read_raw(handle)
		if raw is None:
			raise DataBridgeError(errors.SOURCE_READ_ERROR, "The file is empty")
		try:
			first = self._parse(raw)
		except UnicodeDecodeError as exc:
			raise DataBridgeError(
				errors.SOURCE_READ_ERROR, f"File is not valid {self._encoding()}: {exc.reason}"
			) from None
		except csv.Error as exc:
			raise DataBridgeError(errors.SOURCE_READ_ERROR, f"CSV error in header: {exc}") from None
		if self._has_header():
			return unique_headers(first), consumed
		return [f"col_{i}" for i in range(1, len(first) + 1)], 0

	def test(self) -> None:
		with self._open() as handle:
			self._header_and_start(handle)

	def list_objects(self) -> list[str]:
		return ["default"]

	def list_fields(self, source_object: str) -> list[str]:
		with self._open() as handle:
			return self._header_and_start(handle)[0]

	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		rows: list[SourceRow] = []
		with self._open() as handle:
			header, start = self._header_and_start(handle)
			offset = int(cursor["offset"]) if cursor else start
			row_no = int(cursor["row"]) if cursor else 0
			handle.seek(offset)
			done = False
			while len(rows) < batch_size:
				raw, consumed = self._read_raw(handle)
				if raw is None:
					done = True
					break
				offset += consumed
				try:
					fields = self._parse(raw)
				except (UnicodeDecodeError, csv.Error) as exc:
					row_no += 1
					rows.append(SourceRow({}, row_no, error=f"Unreadable record: {type(exc).__name__}"))
					continue
				if not fields or all(not f.strip() for f in fields):
					continue  # blank line
				row_no += 1
				padded = (fields + [None] * len(header))[: len(header)]
				rows.append(SourceRow(dict(zip(header, padded, strict=True)), row_no))
			if not done:
				done = handle.peek(1) == b""
		return Batch(rows=rows, cursor={"offset": offset, "row": row_no}, done=done)


class TsvConnector(CsvConnector):
	name = "TSV"
	default_delimiter = "\t"
