"""XLSX via openpyxl read-only mode (rows are parsed lazily, the sheet is never fully loaded)."""

from __future__ import annotations

import zipfile
from typing import Any

from databridge.connectors.base import Batch, Connector, SourceRow, unique_headers
from databridge.connectors.csv_connector import require_path
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError


class XlsxConnector(Connector):
	name = "XLSX"

	def _workbook(self):
		from openpyxl import load_workbook

		path = require_path(self.config)
		try:
			return load_workbook(path, read_only=True, data_only=True)
		except (zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
			raise DataBridgeError(
				errors.SOURCE_READ_ERROR, f"Not a readable XLSX file: {type(exc).__name__}"
			) from None

	def _sheet(self, workbook, source_object: str):
		wanted = (
			source_object
			if source_object and source_object != "default"
			else self.config.options.get("sheet_name")
		)
		if not wanted:
			return workbook.worksheets[0]
		if wanted not in workbook.sheetnames:
			raise DataBridgeError(errors.INVALID_INPUT, f"Sheet {wanted!r} does not exist in the workbook")
		return workbook[wanted]

	def _has_header(self) -> bool:
		return bool(int(self.config.options.get("has_header", 1)))

	def test(self) -> None:
		workbook = self._workbook()
		try:
			if not workbook.worksheets:
				raise DataBridgeError(errors.SOURCE_READ_ERROR, "The workbook has no sheets")
		finally:
			workbook.close()

	def list_objects(self) -> list[str]:
		workbook = self._workbook()
		try:
			return list(workbook.sheetnames)
		finally:
			workbook.close()

	def _header(self, sheet) -> list[str]:
		first = next(sheet.iter_rows(min_row=1, max_row=1, values_only=True), None)
		if first is None:
			raise DataBridgeError(errors.SOURCE_READ_ERROR, "The sheet is empty")
		if self._has_header():
			return unique_headers(list(first))
		return [f"col_{i}" for i in range(1, len(first) + 1)]

	def list_fields(self, source_object: str) -> list[str]:
		workbook = self._workbook()
		try:
			return self._header(self._sheet(workbook, source_object))
		finally:
			workbook.close()

	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		workbook = self._workbook()
		try:
			sheet = self._sheet(workbook, source_object)
			header = self._header(sheet)
			next_row = int(cursor["row"]) if cursor else (2 if self._has_header() else 1)
			rows: list[SourceRow] = []
			done = True
			cursor_row = next_row
			for row_idx, values in enumerate(
				sheet.iter_rows(min_row=next_row, values_only=True), start=next_row
			):
				if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
					cursor_row = row_idx + 1
					continue
				if len(rows) >= batch_size:
					done = False
					cursor_row = row_idx
					break
				padded = (list(values) + [None] * len(header))[: len(header)]
				rows.append(SourceRow(dict(zip(header, padded, strict=True)), row_idx))
				cursor_row = row_idx + 1
			return Batch(rows=rows, cursor={"row": cursor_row}, done=done)
		finally:
			workbook.close()
