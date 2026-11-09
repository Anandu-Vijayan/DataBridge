"""Validate and coerce mapped values against the target DocType's field specs.

Pure: the target meta is flattened into FieldSpec objects at the edge (build_field_specs), and link
existence is an injected callable, so everything here runs without a site.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

LinkChecker = Callable[[str, str], bool]

INT_TYPES = {"Int", "Check", "Long Int"}
FLOAT_TYPES = {"Float", "Currency", "Percent", "Rating"}
DATE_TYPES = {"Date"}
DATETIME_TYPES = {"Datetime"}
TIME_TYPES = {"Time"}
SHORT_TEXT_TYPES = {"Data", "Link", "Select", "Phone", "Autocomplete", "Barcode", "Color"}
DEFAULT_DATA_LENGTH = 140
NAME_LENGTH = 140


@dataclass(frozen=True)
class FieldSpec:
	fieldname: str
	fieldtype: str
	label: str = ""
	reqd: bool = False
	options: str = ""
	length: int = 0
	has_default: bool = False
	read_only: bool = False
	hidden: bool = False


def _fail(code: str, spec: FieldSpec, message: str) -> DataBridgeError:
	return DataBridgeError(code, f"{spec.fieldname}: {message}")


def _coerce_int(value: Any, spec: FieldSpec) -> int:
	try:
		if isinstance(value, bool):
			return int(value)
		if isinstance(value, float) and not value.is_integer():
			raise ValueError
		return int(value)
	except (TypeError, ValueError):
		raise _fail(errors.VALIDATION_ERROR, spec, f"{value!r} is not a whole number") from None


def _coerce_float(value: Any, spec: FieldSpec) -> float:
	try:
		return float(value)
	except (TypeError, ValueError):
		raise _fail(errors.VALIDATION_ERROR, spec, f"{value!r} is not a number") from None


def _coerce_date(value: Any, spec: FieldSpec) -> str:
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d")
	if isinstance(value, date):
		return value.isoformat()
	try:
		return date.fromisoformat(str(value).strip()[:10]).isoformat()
	except ValueError:
		raise _fail(errors.VALIDATION_ERROR, spec, f"{value!r} is not a date (expected YYYY-MM-DD)") from None


def _coerce_datetime(value: Any, spec: FieldSpec) -> str:
	if isinstance(value, datetime):
		return value.strftime("%Y-%m-%d %H:%M:%S")
	if isinstance(value, date):
		return f"{value.isoformat()} 00:00:00"
	try:
		return datetime.fromisoformat(str(value).strip()).strftime("%Y-%m-%d %H:%M:%S")
	except ValueError:
		raise _fail(errors.VALIDATION_ERROR, spec, f"{value!r} is not a datetime") from None


def coerce_value(value: Any, spec: FieldSpec, link_exists: LinkChecker | None = None) -> Any:
	"""Coerce one non-empty value to the field's type. Raises a record-level DataBridgeError."""
	fieldtype = spec.fieldtype
	if fieldtype in INT_TYPES:
		result: Any = _coerce_int(value, spec)
		if fieldtype == "Check" and result not in (0, 1):
			raise _fail(errors.VALIDATION_ERROR, spec, "a checkbox must be 0 or 1")
		return result
	if fieldtype in FLOAT_TYPES:
		return _coerce_float(value, spec)
	if fieldtype in DATE_TYPES:
		return _coerce_date(value, spec)
	if fieldtype in DATETIME_TYPES:
		return _coerce_datetime(value, spec)
	if fieldtype in TIME_TYPES:
		return str(value).strip()

	text = value if isinstance(value, str) else str(value)
	if fieldtype == "Select":
		allowed = [o.strip() for o in spec.options.split("\n") if o.strip()]
		if allowed and text.strip() not in allowed:
			raise _fail(errors.INVALID_OPTION, spec, f"{text!r} is not one of: {', '.join(allowed)}")
		return text.strip()
	if fieldtype == "Link":
		text = text.strip()
		if link_exists is not None and spec.options and not link_exists(spec.options, text):
			raise _fail(errors.LINK_NOT_FOUND, spec, f"{spec.options} {text!r} does not exist")
		return text
	if fieldtype in SHORT_TEXT_TYPES:
		limit = spec.length or DEFAULT_DATA_LENGTH
		if len(text) > limit:
			raise _fail(errors.VALUE_TOO_LONG, spec, f"value is {len(text)} characters, the limit is {limit}")
	return text


def validate_values(
	values: Mapping[str, Any],
	specs: Mapping[str, FieldSpec],
	link_exists: LinkChecker | None = None,
	*,
	check_required: bool = True,
) -> dict[str, Any]:
	"""Return coerced values, or raise on the first problem.

	check_required should be False for updates, where unmapped fields keep their current value.
	"""
	result: dict[str, Any] = {}
	for fieldname, value in values.items():
		if fieldname == "name":
			if value is None:
				continue
			name = str(value).strip()
			if len(name) > NAME_LENGTH:
				raise DataBridgeError(errors.VALUE_TOO_LONG, f"name: longer than {NAME_LENGTH} characters")
			result["name"] = name
			continue
		spec = specs.get(fieldname)
		if spec is None:
			raise DataBridgeError(
				errors.VALIDATION_ERROR, f"{fieldname}: not a writable field on the target DocType"
			)
		if spec.fieldtype == "Table":
			raise _fail(errors.VALIDATION_ERROR, spec, "child tables cannot be mapped in this version")
		if value is None or (isinstance(value, str) and not value.strip()):
			result[fieldname] = None
			continue
		result[fieldname] = coerce_value(value, spec, link_exists)

	if check_required:
		for spec in specs.values():
			if spec.reqd and not spec.has_default and not spec.read_only and not spec.hidden:
				if result.get(spec.fieldname) in (None, ""):
					raise _fail(errors.REQUIRED_FIELD_MISSING, spec, "is required")
	return result


def build_field_specs(meta: Any) -> dict[str, FieldSpec]:
	"""Flatten a frappe Meta into FieldSpecs for value fields (edge function, needs frappe)."""
	from frappe.model import no_value_fields

	specs: dict[str, FieldSpec] = {}
	for df in meta.fields:
		if df.fieldtype in no_value_fields and df.fieldtype != "Table":
			continue
		if getattr(df, "is_virtual", 0):
			continue
		specs[df.fieldname] = FieldSpec(
			fieldname=df.fieldname,
			fieldtype=df.fieldtype,
			label=df.label or df.fieldname,
			reqd=bool(df.reqd),
			options=df.options or "",
			length=int(df.length or 0),
			has_default=df.default not in (None, ""),
			read_only=bool(df.read_only),
			hidden=bool(df.hidden),
		)
	return specs
