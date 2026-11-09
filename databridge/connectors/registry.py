"""Connector registry. Phase 2 sources (REST, SOAP, ...) register here without touching the runner."""

from __future__ import annotations

from importlib import import_module

from databridge.connectors.base import ConnectionConfig, Connector
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

# connector name (as stored on the Connection DocType) -> "module:Class"
_BUILTIN: dict[str, str] = {
	"CSV": "databridge.connectors.csv_connector:CsvConnector",
	"TSV": "databridge.connectors.csv_connector:TsvConnector",
	"XLSX": "databridge.connectors.xlsx_connector:XlsxConnector",
	"JSON": "databridge.connectors.json_connector:JsonConnector",
	"JSONL": "databridge.connectors.json_connector:JsonlConnector",
	"XML": "databridge.connectors.xml_connector:XmlConnector",
	"MySQL": "databridge.connectors.mysql_connector:MySQLConnector",
}
_CUSTOM: dict[str, type[Connector]] = {}


def register(name: str, cls: type[Connector]) -> None:
	_CUSTOM[name] = cls


def supported() -> list[str]:
	return sorted({*_BUILTIN, *_CUSTOM})


def get_connector_class(name: str) -> type[Connector]:
	if name in _CUSTOM:
		return _CUSTOM[name]
	target = _BUILTIN.get(name)
	if not target:
		raise DataBridgeError(errors.UNSUPPORTED_SOURCE, f"Unsupported source type {name!r}")
	module_name, class_name = target.split(":")
	return getattr(import_module(module_name), class_name)


def build_connector(config: ConnectionConfig) -> Connector:
	return get_connector_class(config.connector)(config)
