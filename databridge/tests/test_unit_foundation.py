from __future__ import annotations

import pytest

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.utils import redact, response


def test_error_str_contains_code():
	assert str(DataBridgeError(errors.INVALID_INPUT, "bad")) == "[INVALID_INPUT] bad"


def test_redact_text_removes_secret():
	assert redact.redact_text("login failed for pw s3cret", ["s3cret"]) == "login failed for pw ***"


def test_redact_text_ignores_empty_secret():
	assert redact.redact_text("abc", [None, ""]) == "abc"


def test_redact_value_nested():
	data = {"host": "h", "password": "p", "opts": [{"api_key": "k", "x": "has p inside"}]}
	out = redact.redact_value(data, ["p"])
	assert out["password"] == "***"
	assert out["opts"][0]["api_key"] == "***"
	assert out["opts"][0]["x"] == "has *** inside"


def test_ok_and_fail_envelope():
	assert response.ok(1) == {"ok": True, "data": 1, "error": None}
	assert response.fail("X", "m") == {"ok": False, "data": None, "error": {"code": "X", "message": "m"}}


def test_api_endpoint_converts_databridge_error():
	@response.api_endpoint
	def boom(a: int) -> None:
		raise DataBridgeError(errors.NOT_FOUND, "nope")

	assert boom(1)["error"]["code"] == errors.NOT_FOUND
	assert list(__import__("inspect").signature(boom).parameters) == ["a"]


def test_clamp_page_caps_and_validates():
	assert response.clamp_page(None, None) == (0, 20)
	assert response.clamp_page("5", "1000") == (5, response.MAX_PAGE_LENGTH)
	with pytest.raises(DataBridgeError):
		response.clamp_page("x", 1)
	with pytest.raises(DataBridgeError):
		response.clamp_page(-1, 1)


def test_require_str():
	assert response.require_str(" a ", "n") == "a"
	with pytest.raises(DataBridgeError):
		response.require_str("", "n")
