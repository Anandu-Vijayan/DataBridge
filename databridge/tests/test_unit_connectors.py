"""Connector tests. Files are generated in tmp_path, MySQL is exercised through a fake connection."""

from __future__ import annotations

import datetime as dt

import pytest

from databridge.connectors import json_connector
from databridge.connectors.base import ConnectionConfig, to_jsonable, unique_headers
from databridge.connectors.mysql_connector import MySQLConnector, quote_identifier
from databridge.connectors.registry import build_connector, supported
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError


def cfg(kind: str, path=None, **options) -> ConnectionConfig:
	if path is not None:
		options["path"] = str(path)
	return ConnectionConfig(connector=kind, options=options)


def read_all(connector, obj="default", batch_size=2):
	"""Read every batch, resuming from the returned cursor each time. Returns (rows, batches)."""
	rows, cursor, batches = [], None, 0
	while True:
		batch = connector.read_batch(obj, cursor, batch_size)
		rows.extend(batch.rows)
		cursor = batch.cursor
		batches += 1
		assert batches < 100, "runaway pagination"
		if batch.done:
			return rows, batches


def code_of(fn, *a, **k):
	with pytest.raises(DataBridgeError) as info:
		fn(*a, **k)
	return info.value.code


# ---------------------------------------------------------------- base / registry
def test_password_not_in_repr():
	assert "s3cret" not in repr(ConnectionConfig("MySQL", {"host": "h"}, password="s3cret"))


def test_unique_headers():
	assert unique_headers(["a", "a", None, " b "]) == ["a", "a_2", "col_3", "b"]


def test_to_jsonable():
	out = to_jsonable({"d": dt.date(2024, 1, 2), "b": b"\x00\x01", "n": float("nan"), "l": [dt.time(1, 2)]})
	assert out == {"d": "2024-01-02", "b": "base64:AAE=", "n": None, "l": ["01:02:00"]}


def test_registry_supports_all_phase1_sources():
	assert set(supported()) == {"CSV", "TSV", "XLSX", "JSON", "JSONL", "XML", "MySQL"}
	assert code_of(build_connector, ConnectionConfig("Postgres")) == errors.UNSUPPORTED_SOURCE


# ---------------------------------------------------------------- CSV / TSV
def write_csv(tmp_path, text: str, name="a.csv", encoding="utf-8"):
	path = tmp_path / name
	path.write_bytes(text.encode(encoding))
	return path


def test_csv_streams_and_resumes_in_batches(tmp_path):
	body = "id,name\n" + "".join(f"{i},n{i}\n" for i in range(1, 8))
	conn = build_connector(cfg("CSV", write_csv(tmp_path, body)))
	rows, batches = read_all(conn, batch_size=3)
	assert [r.data["id"] for r in rows] == [str(i) for i in range(1, 8)]
	assert [r.position for r in rows] == list(range(1, 8))
	assert batches == 3


def test_csv_exact_multiple_of_batch_size_terminates(tmp_path):
	conn = build_connector(cfg("CSV", write_csv(tmp_path, "id\n1\n2\n3\n4\n")))
	rows, _ = read_all(conn, batch_size=2)
	assert len(rows) == 4


def test_csv_quoted_newlines_bom_blank_lines_and_crlf(tmp_path):
	text = '﻿id,note\r\n1,"line one\nline two"\r\n\r\n2,"say ""hi"""\r\n'
	conn = build_connector(cfg("CSV", write_csv(tmp_path, text)))
	rows, _ = read_all(conn, batch_size=1)
	assert rows[0].data == {"id": "1", "note": "line one\nline two"}
	assert rows[1].data["note"] == 'say "hi"'
	assert conn.list_fields("default") == ["id", "note"]


def test_csv_short_rows_padded_and_long_rows_truncated(tmp_path):
	conn = build_connector(cfg("CSV", write_csv(tmp_path, "a,b\n1\n1,2,3\n")))
	rows, _ = read_all(conn)
	assert rows[0].data == {"a": "1", "b": None}
	assert rows[1].data == {"a": "1", "b": "2"}


def test_csv_without_header(tmp_path):
	conn = build_connector(cfg("CSV", write_csv(tmp_path, "1,x\n2,y\n"), has_header=0))
	rows, _ = read_all(conn)
	assert rows[1].data == {"col_1": "2", "col_2": "y"}


def test_csv_bad_encoding_is_a_record_error_not_a_crash(tmp_path):
	path = tmp_path / "a.csv"
	path.write_bytes(b"id,name\n1,ok\n2,\xff\xfe\n3,fine\n")
	rows, _ = read_all(build_connector(cfg("CSV", path)))
	assert [r.error is None for r in rows] == [True, False, True]


def test_csv_custom_encoding(tmp_path):
	conn = build_connector(
		cfg("CSV", write_csv(tmp_path, "id,name\n1,café\n", encoding="latin-1"), encoding="latin-1")
	)
	assert read_all(conn)[0][0].data["name"] == "café"


def test_csv_rejects_utf16_and_bad_delimiter(tmp_path):
	path = write_csv(tmp_path, "a\n1\n")
	assert code_of(build_connector(cfg("CSV", path, encoding="utf-16")).test) == errors.INVALID_INPUT
	assert code_of(build_connector(cfg("CSV", path, delimiter=";;")).test) == errors.INVALID_INPUT


def test_csv_missing_and_empty_file(tmp_path):
	assert code_of(build_connector(cfg("CSV", tmp_path / "nope.csv")).test) == errors.SOURCE_FILE_MISSING
	assert code_of(build_connector(cfg("CSV", write_csv(tmp_path, ""))).test) == errors.SOURCE_READ_ERROR


def test_tsv_and_custom_delimiter(tmp_path):
	tsv = build_connector(cfg("TSV", write_csv(tmp_path, "a\tb\n1\t2\n", "a.tsv")))
	assert read_all(tsv)[0][0].data == {"a": "1", "b": "2"}
	semi = build_connector(cfg("CSV", write_csv(tmp_path, "a;b\n1;2\n", "b.csv"), delimiter=";"))
	assert read_all(semi)[0][0].data == {"a": "1", "b": "2"}


# ---------------------------------------------------------------- JSONL / JSON
def test_jsonl_resume_invalid_line_and_flatten(tmp_path):
	path = tmp_path / "a.jsonl"
	path.write_text('{"id": 1, "p": {"city": "X"}}\nnot json\n\n[1,2]\n{"id": 4}\n', encoding="utf-8")
	rows, _ = read_all(build_connector(cfg("JSONL", path)), batch_size=2)
	assert rows[0].data == {"id": 1, "p.city": "X"}
	assert [r.error is not None for r in rows] == [False, True, True, False]
	assert rows[3].position == 4


def test_jsonl_bom(tmp_path):
	path = tmp_path / "a.jsonl"
	path.write_bytes(b'\xef\xbb\xbf{"id": 1}\n')
	assert read_all(build_connector(cfg("JSONL", path)))[0][0].data == {"id": 1}


def test_json_array_batches_and_boundaries(tmp_path, monkeypatch):
	monkeypatch.setattr(json_connector, "CHUNK_CHARS", 7)  # force records to straddle chunk reads
	path = tmp_path / "a.json"
	items = ",\n ".join('{"id": %d, "name": "café %d"}' % (i, i) for i in range(1, 8))
	path.write_text(f"[\n {items}\n]", encoding="utf-8")
	conn = build_connector(cfg("JSON", path))
	rows, _ = read_all(conn, batch_size=3)
	assert [r.data["id"] for r in rows] == list(range(1, 8))
	assert conn.list_fields("default") == ["id", "name"]


def test_json_bare_numbers_split_across_chunks(tmp_path, monkeypatch):
	monkeypatch.setattr(json_connector, "CHUNK_CHARS", 3)
	path = tmp_path / "a.json"
	path.write_text("[12345, 678]", encoding="utf-8")
	rows, _ = read_all(build_connector(cfg("JSON", path)))
	assert [r.error for r in rows] == ["Record is not a JSON object"] * 2


@pytest.mark.parametrize("text", ['{"a": 1}', "[{}, ]", '[{"a": 1} {"b": 2}]', "[{"])
def test_json_structure_errors(tmp_path, text):
	path = tmp_path / "a.json"
	path.write_text(text, encoding="utf-8")
	assert code_of(read_all, build_connector(cfg("JSON", path))) == errors.SOURCE_READ_ERROR


def test_json_empty_array(tmp_path):
	path = tmp_path / "a.json"
	path.write_text("  [ ]  ", encoding="utf-8")
	assert read_all(build_connector(cfg("JSON", path)))[0] == []


# ---------------------------------------------------------------- XLSX
def make_xlsx(tmp_path):
	from openpyxl import Workbook

	wb = Workbook()
	ws = wb.active
	ws.title = "People"
	ws.append(["id", "name", "joined"])
	for i in range(1, 6):
		ws.append([i, f"n{i}", dt.datetime(2024, 1, i)])
	ws.append([None, None, None])
	ws.append([6, "n6", None])
	other = wb.create_sheet("Other")
	other.append(["x"])
	other.append(["y"])
	path = tmp_path / "a.xlsx"
	wb.save(path)
	return path


def test_xlsx_batches_blank_rows_and_dates(tmp_path):
	conn = build_connector(cfg("XLSX", make_xlsx(tmp_path)))
	rows, _ = read_all(conn, batch_size=2)
	assert [r.data["id"] for r in rows] == [1, 2, 3, 4, 5, 6]
	assert rows[0].data["joined"] == dt.datetime(2024, 1, 1)
	assert rows[5].position == 8


def test_xlsx_sheets(tmp_path):
	conn = build_connector(cfg("XLSX", make_xlsx(tmp_path)))
	assert conn.list_objects() == ["People", "Other"]
	assert conn.list_fields("Other") == ["x"]
	assert read_all(conn, "Other")[0][0].data == {"x": "y"}
	assert code_of(conn.list_fields, "Nope") == errors.INVALID_INPUT


def test_xlsx_not_a_workbook(tmp_path):
	path = tmp_path / "a.xlsx"
	path.write_text("hello")
	assert code_of(build_connector(cfg("XLSX", path)).test) == errors.SOURCE_READ_ERROR


# ---------------------------------------------------------------- XML
XML = """<?xml version="1.0"?>
<people>
  <person id="1"><name>Ann</name><city>X</city></person>
  <person id="2"><name>Bob</name><city/></person>
  <person id="3"><name>Cy</name><city>Z</city></person>
</people>"""


def test_xml_records_attributes_resume(tmp_path):
	path = tmp_path / "a.xml"
	path.write_text(XML, encoding="utf-8")
	conn = build_connector(cfg("XML", path, record_tag="person"))
	rows, _ = read_all(conn, batch_size=2)
	assert rows[0].data == {"@id": "1", "name": "Ann", "city": "X"}
	assert rows[1].data["city"] is None
	assert len(rows) == 3
	assert conn.list_fields("person") == ["@id", "name", "city"]


def test_xml_requires_record_tag(tmp_path):
	path = tmp_path / "a.xml"
	path.write_text(XML, encoding="utf-8")
	assert code_of(build_connector(cfg("XML", path)).test) == errors.INVALID_INPUT


def test_xml_refuses_entities_and_dtds(tmp_path):
	bomb = (
		'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]>'
		"<r><p><v>&b;</v></p></r>"
	)
	path = tmp_path / "a.xml"
	path.write_text(bomb, encoding="utf-8")
	assert code_of(build_connector(cfg("XML", path, record_tag="p")).test) == errors.SOURCE_READ_ERROR


def test_xml_malformed(tmp_path):
	path = tmp_path / "a.xml"
	path.write_text("<r><p>", encoding="utf-8")
	assert code_of(read_all, build_connector(cfg("XML", path, record_tag="p"))) == errors.SOURCE_READ_ERROR


# ---------------------------------------------------------------- MySQL (fake connection)
class FakeCursor:
	def __init__(self, conn):
		self.conn = conn
		self.result: list[tuple] = []

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def execute(self, sql, params=()):
		self.conn.executed.append((sql, tuple(params)))
		self.result = self.conn.script(sql, tuple(params))

	def fetchall(self):
		return self.result

	def fetchone(self):
		return self.result[0] if self.result else None


class FakeConn:
	def __init__(self, script):
		self.script = script
		self.executed: list[tuple[str, tuple]] = []
		self.closed = False

	def cursor(self):
		return FakeCursor(self)

	def close(self):
		self.closed = True


def make_mysql(monkeypatch, pk=("id",), data=None, tables=("people", "weird`name")):
	columns = ["id", "name"]
	data = data if data is not None else [(i, f"n{i}") for i in range(1, 6)]

	def script(sql, params):
		if "information_schema.TABLES" in sql:
			return [(t,) for t in tables]
		if "information_schema.COLUMNS" in sql:
			return [(c, "PRI" if c in pk else "") for c in columns]
		if sql.startswith("SELECT `id`"):
			batch_limit = params[-1] if "OFFSET" not in sql else params[-2]
			return data[:batch_limit]
		return []

	conn = FakeConn(script)
	connector = MySQLConnector(ConnectionConfig("MySQL", {"database": "src"}, password="s3cret"))
	monkeypatch.setattr(connector, "_connect", lambda: conn)
	return connector, conn


def test_quote_identifier():
	assert quote_identifier("a`b") == "`a``b`"
	assert code_of(quote_identifier, "") == errors.INVALID_IDENTIFIER
	assert code_of(quote_identifier, "a\x00b") == errors.INVALID_IDENTIFIER


def test_mysql_keyset_pagination_sql_is_parameterised(monkeypatch):
	connector, conn = make_mysql(monkeypatch)
	first = connector.read_batch("people", None, 2)
	assert first.cursor["pk"] == [2] and first.done is False
	connector.read_batch("people", {"pk": [2], "row": 2}, 2)
	selects = [e for e in conn.executed if e[0].startswith("SELECT `id`")]
	assert selects[0] == ("SELECT `id`, `name` FROM `people` ORDER BY `id` LIMIT %s", (2,))
	assert selects[1] == (
		"SELECT `id`, `name` FROM `people` WHERE (`id`) > (%s) ORDER BY `id` LIMIT %s",
		(2, 2),
	)


def test_mysql_last_batch_is_done(monkeypatch):
	connector, _ = make_mysql(monkeypatch, data=[(1, "a")])
	assert connector.read_batch("people", None, 5).done is True


def test_mysql_rejects_table_not_in_schema(monkeypatch):
	connector, conn = make_mysql(monkeypatch)
	for evil in ("people; DROP TABLE x", "people` --", "information_schema.TABLES", ""):
		assert code_of(connector.read_batch, evil, None, 5) == errors.INVALID_IDENTIFIER
	assert not any("DROP" in sql for sql, _ in conn.executed)


def test_mysql_composite_pk_uses_tuple_comparison(monkeypatch):
	connector, conn = make_mysql(monkeypatch, pk=("id", "name"))
	connector.read_batch("people", {"pk": [1, "n1"], "row": 1}, 3)
	sql = [e for e in conn.executed if e[0].startswith("SELECT `id`")][0]
	assert "WHERE (`id`, `name`) > (%s, %s) ORDER BY `id`, `name` LIMIT %s" in sql[0]
	assert sql[1] == (1, "n1", 3)


def test_mysql_without_pk_pages_by_offset(monkeypatch):
	connector, conn = make_mysql(monkeypatch, pk=())
	batch = connector.read_batch("people", {"offset": 4, "row": 4}, 2)
	sql = [e for e in conn.executed if e[0].startswith("SELECT `id`")][0]
	assert sql[0].endswith("ORDER BY `id`, `name` LIMIT %s OFFSET %s")
	assert sql[1] == (2, 4)
	assert batch.cursor["offset"] == 4 + len(batch.rows)


def test_mysql_odd_table_name_is_escaped(monkeypatch):
	connector, conn = make_mysql(monkeypatch)
	connector.read_batch("weird`name", None, 1)
	sql = [e for e in conn.executed if e[0].startswith("SELECT `id`")][0][0]
	assert "FROM `weird``name`" in sql


def test_mysql_default_id_columns_and_discovery(monkeypatch):
	connector, conn = make_mysql(monkeypatch)
	assert connector.default_id_columns("people") == ["id"]
	assert connector.list_fields("people") == ["id", "name"]
	assert connector.list_objects() == ["people", "weird`name"]
	assert conn.closed


def test_mysql_connect_error_redacts_password(monkeypatch):
	import sys
	import types

	class Boom(Exception):
		pass

	fake = types.ModuleType("pymysql")

	def connect(**kwargs):
		raise Boom(f"Access denied using password {kwargs['password']}")

	fake.connect = connect
	monkeypatch.setitem(sys.modules, "pymysql", fake)
	connector = MySQLConnector(ConnectionConfig("MySQL", {"host": "h", "database": "d"}, password="s3cret"))
	with pytest.raises(DataBridgeError) as info:
		connector.test()
	assert info.value.code == errors.CONNECTION_FAILED
	assert "s3cret" not in info.value.message
