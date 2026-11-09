"""Connection APIs. No credentials are ever returned; errors are redacted before they leave."""

import frappe

from databridge.connectors.base import ConnectionConfig, Connector
from databridge.connectors.registry import build_connector
from databridge.engine.errors import DataBridgeError
from databridge.engine.frappe_store import load_connection_config
from databridge.utils.redact import redact_text
from databridge.utils.response import api_endpoint, require_str

CONNECTION = "DataBridge Connection"
MAX_PREVIEW_ROWS = 20


def _open(connection: str, ptype: str) -> tuple[str, Connector, ConnectionConfig]:
	name = require_str(connection, "connection")
	frappe.get_doc(CONNECTION, name).check_permission(ptype)
	config = load_connection_config(name)
	return name, build_connector(config), config


def _record_test(name: str, status: str, error: str | None, config: ConnectionConfig) -> None:
	frappe.db.set_value(
		CONNECTION,
		name,
		{
			"status": status,
			"last_tested": frappe.utils.now_datetime(),
			"last_error": redact_text(error or "", config.secrets())[:500] or None,
		},
		update_modified=False,
	)


@frappe.whitelist(methods=["POST"])
@api_endpoint
def test_connection(connection: str) -> dict:
	"""Check that the source is reachable and readable. Updates the connection's status."""
	name, connector, config = _open(connection, "write")
	try:
		connector.test()
	except DataBridgeError as exc:
		_record_test(name, "Failed", exc.message, config)
		raise
	_record_test(name, "OK", None, config)
	return {"status": "OK"}


@frappe.whitelist(methods=["GET"])
@api_endpoint
def list_source_objects(connection: str) -> list:
	"""Tables, sheets or logical names available in the source (capped at 500)."""
	_, connector, _ = _open(connection, "read")
	return connector.list_objects()[:500]


@frappe.whitelist(methods=["GET"])
@api_endpoint
def list_source_fields(connection: str, source_object: str = "default") -> list:
	"""Column names of one source object."""
	_, connector, _ = _open(connection, "read")
	return connector.list_fields(source_object or "default")


@frappe.whitelist(methods=["GET"])
@api_endpoint
def preview_source(connection: str, source_object: str = "default", limit: int = 10) -> dict:
	"""First few source rows, for eyeballing before mapping. Capped at 20 rows."""
	_, connector, _ = _open(connection, "read")
	rows = connector.preview(source_object or "default", min(int(limit or 10), MAX_PREVIEW_ROWS))
	return {"fields": list(rows[0]) if rows else [], "rows": rows}
