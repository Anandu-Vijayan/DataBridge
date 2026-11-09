"""Validator, reference key and write-decision tests (all pure)."""

from __future__ import annotations

import pytest

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.references import make_ref_key
from databridge.engine.validator import FieldSpec, coerce_value, validate_values
from databridge.engine.writer import FAIL, INSERT, SKIP, UPDATE, decide


def code_of(fn, *a, **k):
	with pytest.raises(DataBridgeError) as info:
		fn(*a, **k)
	return info.value.code


SPECS = {
	"title": FieldSpec("title", "Data", length=10, reqd=True),
	"qty": FieldSpec("qty", "Int"),
	"rate": FieldSpec("rate", "Currency"),
	"due": FieldSpec("due", "Date"),
	"at": FieldSpec("at", "Datetime"),
	"flag": FieldSpec("flag", "Check"),
	"prio": FieldSpec("prio", "Select", options="Low\nHigh"),
	"owner": FieldSpec("owner", "Link", options="User"),
	"note": FieldSpec("note", "Text"),
	"status": FieldSpec("status", "Data", reqd=True, has_default=True),
	"calc": FieldSpec("calc", "Data", reqd=True, read_only=True),
	"rows": FieldSpec("rows", "Table", options="Row"),
}


def test_coercions():
	assert coerce_value("5", SPECS["qty"]) == 5
	assert coerce_value(5.0, SPECS["qty"]) == 5
	assert coerce_value("1.5", SPECS["rate"]) == 1.5
	assert coerce_value("2024-03-04T10:00:00", SPECS["due"]) == "2024-03-04"
	assert coerce_value("2024-03-04T10:00:00", SPECS["at"]) == "2024-03-04 10:00:00"
	assert coerce_value(1, SPECS["flag"]) == 1
	assert coerce_value("High", SPECS["prio"]) == "High"
	assert coerce_value(12, SPECS["note"]) == "12"


@pytest.mark.parametrize(
	("field", "value", "code"),
	[
		("qty", "x", errors.VALIDATION_ERROR),
		("qty", 1.5, errors.VALIDATION_ERROR),
		("rate", "abc", errors.VALIDATION_ERROR),
		("due", "04/03/2024", errors.VALIDATION_ERROR),
		("at", "yesterday", errors.VALIDATION_ERROR),
		("flag", 2, errors.VALIDATION_ERROR),
		("prio", "Medium", errors.INVALID_OPTION),
		("title", "x" * 11, errors.VALUE_TOO_LONG),
	],
)
def test_coercion_errors(field, value, code):
	assert code_of(coerce_value, value, SPECS[field]) == code


def test_link_check_uses_injected_callable():
	seen = []

	def exists(dt, name):
		seen.append((dt, name))
		return name == "a@x.com"

	assert coerce_value(" a@x.com ", SPECS["owner"], exists) == "a@x.com"
	assert code_of(coerce_value, "b@x.com", SPECS["owner"], exists) == errors.LINK_NOT_FOUND
	assert seen[0] == ("User", "a@x.com")


def test_validate_values_full():
	out = validate_values({"title": "T", "qty": "3", "note": ""}, SPECS, check_required=True)
	assert out == {"title": "T", "qty": 3, "note": None}


def test_required_enforced_only_when_asked_and_respects_defaults_and_readonly():
	assert code_of(validate_values, {"qty": 1}, SPECS) == errors.REQUIRED_FIELD_MISSING
	assert validate_values({"qty": 1}, SPECS, check_required=False) == {"qty": 1}
	assert code_of(validate_values, {"title": ""}, SPECS) == errors.REQUIRED_FIELD_MISSING


def test_unknown_table_and_name_fields():
	assert code_of(validate_values, {"ghost": 1}, SPECS, check_required=False) == errors.VALIDATION_ERROR
	assert code_of(validate_values, {"rows": "x"}, SPECS, check_required=False) == errors.VALIDATION_ERROR
	assert validate_values({"name": " DOC-1 "}, SPECS, check_required=False) == {"name": "DOC-1"}
	assert code_of(validate_values, {"name": "n" * 141}, SPECS, check_required=False) == errors.VALUE_TOO_LONG


def test_ref_key_is_stable_distinct_and_fixed_length():
	a = make_ref_key("C", "t", "1", "Customer")
	assert a == make_ref_key("C", "t", "1", "Customer")
	assert len(a) == 40
	assert (
		len(
			{
				a,
				make_ref_key("C", "t", "2", "Customer"),
				make_ref_key("C", "u", "1", "Customer"),
				make_ref_key("D", "t", "1", "Customer"),
				make_ref_key("C", "t", "1", "Item"),
			}
		)
		== 5
	)
	# separator prevents ("ab","c") colliding with ("a","bc")
	assert make_ref_key("ab", "c", "1", "X") != make_ref_key("a", "bc", "1", "X")


@pytest.mark.parametrize(
	("mode", "exists", "expected"),
	[
		("Insert", False, INSERT),
		("Insert", True, SKIP),
		("Skip", False, INSERT),
		("Skip", True, SKIP),
		("Update", True, UPDATE),
		("Update", False, FAIL),
		("Upsert", True, UPDATE),
		("Upsert", False, INSERT),
	],
)
def test_decision_table(mode, exists, expected):
	assert decide(mode, exists) == expected


def test_decide_rejects_unknown_mode():
	assert code_of(decide, "Merge", True) == errors.MAPPING_INVALID
