"""XML via defusedxml only (DTDs, entities and external references are refused).

Records are the repeating element named by the connection's record_tag. Each record becomes a flat
dict: child element text by tag name, record attributes as "@name". Nested children are ignored,
and a repeated child tag keeps its last value. A resume re-parses (without keeping) the records
before the cursor.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from databridge.connectors.base import Batch, Connector, SourceRow
from databridge.connectors.csv_connector import require_path
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1]


def element_to_dict(element: Any) -> dict[str, Any]:
	data: dict[str, Any] = {f"@{_local(k)}": v for k, v in element.attrib.items()}
	for child in element:
		if len(child) == 0:
			data[_local(child.tag)] = (child.text or "").strip() or None
	return data


class XmlConnector(Connector):
	name = "XML"

	def _record_tag(self) -> str:
		tag = (self.config.options.get("record_tag") or "").strip()
		if not tag:
			raise DataBridgeError(errors.INVALID_INPUT, "Record tag is required for XML sources")
		return tag

	def _iter_records(self) -> Iterator[dict[str, Any]]:
		tag = self._record_tag()
		path = require_path(self.config)
		with open(path, "rb") as handle:
			root = None
			try:
				for event, element in SafeET.iterparse(
					handle,
					events=("start", "end"),
					forbid_dtd=True,
					forbid_entities=True,
					forbid_external=True,
				):
					if root is None:
						root = element
					if event == "end" and _local(element.tag) == tag:
						yield element_to_dict(element)
						element.clear()
						root.clear()  # drop finished siblings so memory stays flat
			except DefusedXmlException:
				raise DataBridgeError(
					errors.SOURCE_READ_ERROR, "The XML uses forbidden constructs (DTD or entities)"
				) from None
			except SafeET.ParseError as exc:
				raise DataBridgeError(errors.SOURCE_READ_ERROR, f"Invalid XML: {exc}") from None

	def test(self) -> None:
		next(self._iter_records(), None)

	def list_objects(self) -> list[str]:
		return [self._record_tag()]

	def list_fields(self, source_object: str) -> list[str]:
		fields: dict[str, None] = {}
		for index, record in enumerate(self._iter_records()):
			fields.update(dict.fromkeys(record))
			if index >= 49:
				break
		return list(fields)

	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		skip = int(cursor["row"]) if cursor else 0
		rows: list[SourceRow] = []
		done = True
		records = self._iter_records()
		try:
			for position, record in enumerate(records, start=1):
				if position <= skip:
					continue
				if len(rows) >= batch_size:
					done = False
					break
				rows.append(SourceRow(record, position))
		finally:
			records.close()
		return Batch(rows=rows, cursor={"row": skip + len(rows)}, done=done)
