"""Secret redaction. Every log line, error message and API payload goes through here."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

REDACTED = "***"

_SENSITIVE_KEY_PARTS = (
	"password",
	"passwd",
	"secret",
	"token",
	"api_key",
	"apikey",
	"private_key",
	"authorization",
)


def is_sensitive_key(key: str) -> bool:
	lowered = str(key).lower()
	return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_text(text: str, secrets: Iterable[str | None] = ()) -> str:
	"""Replace every occurrence of a known secret value in free text."""
	result = str(text)
	for secret in secrets:
		if secret:
			result = result.replace(str(secret), REDACTED)
	return result


def redact_value(value: Any, secrets: Iterable[str | None] = ()) -> Any:
	"""Recursively redact: sensitive keys lose their value, known secrets vanish from strings."""
	secrets = tuple(s for s in secrets if s)
	if isinstance(value, dict):
		return {
			k: (REDACTED if is_sensitive_key(k) and v not in (None, "") else redact_value(v, secrets))
			for k, v in value.items()
		}
	if isinstance(value, list | tuple):
		return [redact_value(v, secrets) for v in value]
	if isinstance(value, str):
		return redact_text(value, secrets)
	return value


def safe_error_message(exc: BaseException, secrets: Iterable[str | None] = ()) -> str:
	"""Message for an arbitrary exception, with secrets removed and length capped."""
	return redact_text(str(exc), secrets)[:1000]
