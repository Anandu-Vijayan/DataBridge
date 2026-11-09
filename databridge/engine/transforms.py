"""Value transforms. Small pure functions, no I/O.

Formulas never run arbitrary Python: they go through frappe.safe_eval with a tiny context
(value, row). The evaluator is injectable so unit tests do not need a site.
"""

from __future__ import annotations

import ast
import html
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

Evaluator = Callable[[str, Mapping[str, Any]], Any]

MAX_FORMULA_LENGTH = 500
_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}
_TAG_RE = re.compile(r"<[^>]+>")


def is_empty(value: Any) -> bool:
	return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str:
	return value if isinstance(value, str) else str(value)


def _trim(value: Any, args: str) -> Any:
	return value.strip() if isinstance(value, str) else value


def _upper(value: Any, args: str) -> Any:
	return value.upper() if isinstance(value, str) else value


def _lower(value: Any, args: str) -> Any:
	return value.lower() if isinstance(value, str) else value


def _title(value: Any, args: str) -> Any:
	return value.title() if isinstance(value, str) else value


def _int(value: Any, args: str) -> int:
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, int):
		return value
	try:
		number = Decimal(_text(value).strip().replace(",", ""))
	except InvalidOperation:
		raise DataBridgeError(errors.TRANSFORM_ERROR, f"Cannot convert {value!r} to an integer") from None
	if number != number.to_integral_value():
		raise DataBridgeError(errors.TRANSFORM_ERROR, f"{value!r} is not a whole number")
	return int(number)


def _float(value: Any, args: str) -> float:
	if isinstance(value, int | float) and not isinstance(value, bool):
		return float(value)
	try:
		return float(Decimal(_text(value).strip().replace(",", "")))
	except InvalidOperation:
		raise DataBridgeError(errors.TRANSFORM_ERROR, f"Cannot convert {value!r} to a number") from None


def _bool(value: Any, args: str) -> int:
	if isinstance(value, bool | int | float):
		return 1 if value else 0
	text = _text(value).strip().lower()
	if text in _TRUE:
		return 1
	if text in _FALSE:
		return 0
	raise DataBridgeError(errors.TRANSFORM_ERROR, f"Cannot convert {value!r} to a yes/no value")


def _parse_datetime(value: Any, args: str) -> datetime:
	if isinstance(value, datetime):
		return value
	if isinstance(value, date):
		return datetime(value.year, value.month, value.day)
	text = _text(value).strip()
	try:
		if args:
			return datetime.strptime(text, args)
		return datetime.fromisoformat(text)
	except ValueError:
		hint = f" using format {args}" if args else " (expected ISO format or a format argument)"
		raise DataBridgeError(errors.TRANSFORM_ERROR, f"Cannot parse {value!r} as a date{hint}") from None


def _date(value: Any, args: str) -> str:
	return _parse_datetime(value, args).strftime("%Y-%m-%d")


def _datetime(value: Any, args: str) -> str:
	return _parse_datetime(value, args).strftime("%Y-%m-%d %H:%M:%S")


def _replace(value: Any, args: str) -> Any:
	"""args: 'old=>new;old2=>new2'. Applied in order, plain text (no regex)."""
	if not isinstance(value, str):
		return value
	for pair in (p for p in args.split(";") if p):
		if "=>" not in pair:
			raise DataBridgeError(
				errors.TRANSFORM_ERROR, "replace arguments must look like old=>new;old2=>new2"
			)
		old, new = pair.split("=>", 1)
		value = value.replace(old, new)
	return value


def _null_if_empty(value: Any, args: str) -> Any:
	return None if is_empty(value) else value


def _strip_html(value: Any, args: str) -> Any:
	return html.unescape(_TAG_RE.sub("", value)).strip() if isinstance(value, str) else value


TRANSFORMS: dict[str, Callable[[Any, str], Any]] = {
	"trim": _trim,
	"upper": _upper,
	"lower": _lower,
	"title": _title,
	"int": _int,
	"float": _float,
	"bool": _bool,
	"date": _date,
	"datetime": _datetime,
	"replace": _replace,
	"null_if_empty": _null_if_empty,
	"strip_html": _strip_html,
}
FORMULA = "formula"
TRANSFORM_NAMES = (*TRANSFORMS, FORMULA)


def check_formula(formula: str) -> str | None:
	"""Static pre-check used when saving a mapping. Returns a problem description or None."""
	if not formula or not formula.strip():
		return "Formula is empty"
	if len(formula) > MAX_FORMULA_LENGTH:
		return f"Formula is longer than {MAX_FORMULA_LENGTH} characters"
	if "__" in formula:
		return "Formula must not contain double underscores"
	try:
		ast.parse(formula.strip(), mode="eval")
	except SyntaxError as exc:
		return f"Formula syntax error: {exc.msg}"
	return None


def frappe_evaluator(code: str, context: Mapping[str, Any]) -> Any:
	"""Default evaluator: frappe.safe_eval with a restricted context. Imported lazily."""
	import frappe

	return frappe.safe_eval(code, None, dict(context))


def evaluate_formula(
	formula: str, value: Any, row: Mapping[str, Any], evaluator: Evaluator | None = None
) -> Any:
	problem = check_formula(formula)
	if problem:
		raise DataBridgeError(errors.FORMULA_ERROR, problem)
	run = evaluator or frappe_evaluator
	try:
		return run(formula.strip(), {"value": value, "row": dict(row)})
	except DataBridgeError:
		raise
	except Exception as exc:
		raise DataBridgeError(errors.FORMULA_ERROR, f"Formula failed: {type(exc).__name__}: {exc}") from None


def apply_transform(
	name: str | None,
	value: Any,
	args: str | None = "",
	*,
	formula: str | None = None,
	row: Mapping[str, Any] | None = None,
	evaluator: Evaluator | None = None,
) -> Any:
	"""Apply one transform. Empty input stays empty except for formulas, which see the raw value."""
	if not name:
		return value
	if name == FORMULA:
		return evaluate_formula(formula or "", value, row or {}, evaluator)
	func = TRANSFORMS.get(name)
	if func is None:
		raise DataBridgeError(errors.TRANSFORM_ERROR, f"Unknown transform {name!r}")
	if is_empty(value) and name != "null_if_empty":
		return None
	return func(value, (args or "").strip())
