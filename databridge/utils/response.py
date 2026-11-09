"""Uniform envelope for every whitelisted API: {"ok", "data", "error": {"code", "message"} | None}."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.utils.redact import safe_error_message

MAX_PAGE_LENGTH = 100


def ok(data: Any = None) -> dict[str, Any]:
	return {"ok": True, "data": data, "error": None}


def fail(code: str, message: str) -> dict[str, Any]:
	return {"ok": False, "data": None, "error": {"code": code, "message": message}}


def api_endpoint(func: Callable[..., Any]) -> Callable[..., dict[str, Any]]:
	"""Wrap an API function so it always returns the envelope and never leaks a traceback."""

	@functools.wraps(func)
	def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
		try:
			return ok(func(*args, **kwargs))
		except DataBridgeError as exc:
			return fail(exc.code, exc.message)
		except Exception as exc:
			import frappe

			if isinstance(exc, frappe.PermissionError):
				return fail(errors.PERMISSION_DENIED, "You do not have permission for this action.")
			if isinstance(exc, frappe.DoesNotExistError):
				return fail(errors.NOT_FOUND, "The requested document does not exist.")
			frappe.log_error(title="DataBridge API error")
			return fail(errors.INTERNAL_ERROR, safe_error_message(exc))

	# Frappe filters request args by the function's own argspec; expose the real signature.
	wrapper.__signature__ = inspect.signature(func)  # type: ignore[attr-defined]
	return wrapper


def clamp_page(start: Any, page_length: Any) -> tuple[int, int]:
	"""Validate pagination input. Raises INVALID_INPUT for non-integers."""
	try:
		start_i = int(start or 0)
		length_i = int(page_length or 20)
	except (TypeError, ValueError):
		raise DataBridgeError(errors.INVALID_INPUT, "start and page_length must be integers") from None
	if start_i < 0 or length_i < 1:
		raise DataBridgeError(errors.INVALID_INPUT, "start must be >= 0 and page_length >= 1")
	return start_i, min(length_i, MAX_PAGE_LENGTH)


def require_str(value: Any, name: str) -> str:
	if not isinstance(value, str) or not value.strip():
		raise DataBridgeError(errors.INVALID_INPUT, f"{name} is required")
	return value.strip()
