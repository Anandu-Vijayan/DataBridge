"""Static checks on the DocType JSON files. No site needed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOCTYPE_DIR = Path(__file__).resolve().parents[1] / "databridge" / "doctype"
JSON_FILES = sorted(DOCTYPE_DIR.glob("*/*.json"))


def test_seven_doctypes_exist():
	assert len(JSON_FILES) == 7


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: p.stem)
def test_doctype_json_consistent(path: Path):
	doc = json.loads(path.read_text())
	assert doc["module"] == "DataBridge"
	names = [f["fieldname"] for f in doc["fields"]]
	assert names == doc["field_order"]
	assert len(names) == len(set(names)), "duplicate fieldnames"
	assert path.with_suffix(".py").exists()
	if not doc.get("istable"):
		assert doc["permissions"], "non-child doctype needs permissions"


def test_link_targets_are_known():
	known = {json.loads(p.read_text())["name"] for p in JSON_FILES} | {"DocType", "User"}
	for path in JSON_FILES:
		for field in json.loads(path.read_text())["fields"]:
			if field["fieldtype"] in ("Link", "Table"):
				assert field["options"] in known, f"{path.stem}.{field['fieldname']}"
