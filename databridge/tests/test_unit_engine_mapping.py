from __future__ import annotations

import json
from pathlib import Path

import pytest

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import (
	FieldRule,
	build_snapshot,
	extract_source_id,
	map_record,
	spec_from_snapshot,
	validate_rules,
)
from databridge.engine.transforms import TRANSFORM_NAMES, apply_transform, check_formula


def fake_eval(code, ctx):
	"""Stand-in for frappe.safe_eval: tiny, only for tests."""
	return eval(code, {"__builtins__": {}}, dict(ctx))


def code_of(fn, *a, **k):
	with pytest.raises(DataBridgeError) as info:
		fn(*a, **k)
	return info.value.code


def test_transform_options_in_doctype_json_match_registry():
	path = Path(__file__).resolve().parents[1] / "databridge" / "doctype"
	data = json.loads((path / "databridge_mapping_field" / "databridge_mapping_field.json").read_text())
	options = next(f for f in data["fields"] if f["fieldname"] == "transform")["options"].split("\n")
	assert [o for o in options if o] == list(TRANSFORM_NAMES)


@pytest.mark.parametrize(
	("name", "value", "args", "expected"),
	[
		("trim", "  a ", "", "a"),
		("upper", "ab", "", "AB"),
		("lower", "AB", "", "ab"),
		("title", "john smith", "", "John Smith"),
		("int", "1,234", "", 1234),
		("int", "12.0", "", 12),
		("float", "3.5", "", 3.5),
		("bool", "Yes", "", 1),
		("bool", "off", "", 0),
		("date", "25/12/2024", "%d/%m/%Y", "2024-12-25"),
		("date", "2024-12-25", "", "2024-12-25"),
		("datetime", "2024-12-25T10:30:00", "", "2024-12-25 10:30:00"),
		("replace", "a-b-c", "-=>_", "a_b_c"),
		("null_if_empty", "  ", "", None),
		("strip_html", "<p>Hi &amp; bye</p>", "", "Hi & bye"),
	],
)
def test_transforms(name, value, args, expected):
	assert apply_transform(name, value, args) == expected


def test_empty_value_passes_through_as_none():
	assert apply_transform("int", "", "") is None
	assert apply_transform("int", None, "") is None


@pytest.mark.parametrize(
	("name", "value"), [("int", "abc"), ("int", "1.5"), ("float", "x"), ("bool", "maybe"), ("date", "nope")]
)
def test_bad_values_raise_transform_error(name, value):
	assert code_of(apply_transform, name, value, "") == errors.TRANSFORM_ERROR


def test_unknown_transform():
	assert code_of(apply_transform, "nope", "x") == errors.TRANSFORM_ERROR


def test_formula_uses_injected_evaluator():
	assert (
		apply_transform("formula", "5", "", formula="int(value) * 2", row={}, evaluator=lambda c, x: 10) == 10
	)
	out = apply_transform("formula", "a", "", formula="row['x'] + value", row={"x": "b"}, evaluator=fake_eval)
	assert out == "ba"


def test_formula_guards():
	assert check_formula("") is not None
	assert check_formula("a +") is not None
	assert check_formula("value.__class__") is not None
	assert check_formula("x" * 600) is not None
	assert check_formula("value.upper()") is None
	assert code_of(apply_transform, "formula", "v", "", formula="1 +", row={}) == errors.FORMULA_ERROR


def test_formula_runtime_error_is_wrapped():
	def boom(code, ctx):
		raise ZeroDivisionError("x")

	assert (
		code_of(apply_transform, "formula", "v", "", formula="1/0", row={}, evaluator=boom)
		== errors.FORMULA_ERROR
	)


def mapping_doc(**over):
	doc = {
		"name": "M1",
		"connection": "C1",
		"source_object": "default",
		"target_doctype": "ToDo",
		"source_id_field": "id",
		"default_mode": "Insert",
		"match_field": "",
		"fields": [
			{"source_field": "id", "target_field": "name_ref"},
			{"source_field": "desc", "target_field": "description", "transform": "trim"},
			{"source_field": "n", "target_field": "priority", "transform": "int", "default_value": "7"},
		],
	}
	doc.update(over)
	return doc


def test_snapshot_roundtrip_and_isolation():
	doc = mapping_doc()
	snap = build_snapshot(doc)
	json.dumps(snap)  # must be serialisable
	doc["fields"][0]["target_field"] = "CHANGED"
	assert snap["rules"][0]["target_field"] == "name_ref"
	spec = spec_from_snapshot(snap)
	assert spec.rules[1].transform == "trim"
	assert spec.mode == "Insert"


def test_snapshot_mode_override():
	assert build_snapshot(mapping_doc(), mode="Upsert")["mode"] == "Upsert"


def test_malformed_snapshot():
	assert code_of(spec_from_snapshot, {"mapping": "x"}) == errors.MAPPING_INVALID


def test_extract_source_id():
	assert extract_source_id({"id": " 5 "}, "id") == "5"
	assert extract_source_id({"id": 5.0}, "id") == "5"
	assert code_of(extract_source_id, {"id": ""}, "id") == errors.MISSING_SOURCE_ID
	assert code_of(extract_source_id, {}, "id") == errors.MISSING_SOURCE_ID
	assert code_of(extract_source_id, {"id": 1}, "") == errors.MISSING_SOURCE_ID


def test_map_record_applies_rules_and_defaults():
	spec = spec_from_snapshot(build_snapshot(mapping_doc()))
	rec = map_record({"id": "1", "desc": "  hi ", "n": ""}, spec)
	assert rec.source_id == "1"
	assert rec.values == {"name_ref": "1", "description": "hi", "priority": 7}


def test_map_record_error_names_target_field():
	spec = spec_from_snapshot(build_snapshot(mapping_doc()))
	with pytest.raises(DataBridgeError) as info:
		map_record({"id": "1", "n": "abc"}, spec)
	assert info.value.code == errors.TRANSFORM_ERROR
	assert info.value.message.startswith("priority:")


def test_map_record_missing_id():
	spec = spec_from_snapshot(build_snapshot(mapping_doc()))
	assert code_of(map_record, {"desc": "x"}, spec) == errors.MISSING_SOURCE_ID


def test_skip_if_empty():
	doc = mapping_doc()
	doc["fields"][1]["skip_if_empty"] = 1
	spec = spec_from_snapshot(build_snapshot(doc))
	assert "description" not in map_record({"id": "1", "desc": ""}, spec).values


def rules_of(doc):
	return spec_from_snapshot(build_snapshot(doc)).rules


def test_validate_rules_ok():
	targets = {"name_ref", "description", "priority"}
	assert validate_rules(rules_of(mapping_doc()), targets, "Insert") == []


def test_validate_rules_reports_problems():
	doc = mapping_doc()
	doc["fields"].append({"source_field": "x", "target_field": "description"})
	doc["fields"].append({"source_field": "y", "target_field": "ghost", "transform": "bogus"})
	doc["fields"].append({"source_field": "z", "target_field": "priority2", "target_table": "items"})
	problems = validate_rules(rules_of(doc), {"name_ref", "description", "priority", "priority2"}, "Update")
	text = " | ".join(problems)
	assert "mapped twice" in text
	assert "ghost" in text
	assert "unknown transform" in text
	assert "child-table" in text
	assert "Match field is required" in text


def test_validate_rules_empty():
	assert validate_rules([], set(), "Insert")
	assert validate_rules([FieldRule("a", "b")], {"b"}, "Bogus")
