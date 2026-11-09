"""Mapping snapshot, source-id extraction and row-to-document mapping. Pure functions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.engine.transforms import (
	FORMULA,
	TRANSFORM_NAMES,
	Evaluator,
	apply_transform,
	check_formula,
	is_empty,
)

MODES = ("Insert", "Update", "Upsert", "Skip")
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class FieldRule:
	source_field: str
	target_field: str
	transform: str = ""
	transform_args: str = ""
	formula: str = ""
	default_value: str = ""
	skip_if_empty: bool = False
	target_table: str = ""  # reserved for child-table mapping, must be empty in Phase 1


@dataclass(frozen=True)
class MappingSpec:
	mapping: str
	connection: str
	source_object: str
	target_doctype: str
	source_id_field: str
	mode: str
	match_field: str
	rules: tuple[FieldRule, ...] = field(default_factory=tuple)
	schema_version: int = SNAPSHOT_VERSION


@dataclass
class MappedRecord:
	source_id: str
	values: dict[str, Any]


def _s(value: Any) -> str:
	return "" if value is None else str(value)


def build_snapshot(mapping: Mapping[str, Any], mode: str | None = None) -> dict[str, Any]:
	"""Freeze a Mapping document (as dict) into a JSON-serialisable snapshot.

	The snapshot is stored on the job at run start, so later edits to the Mapping never change a run.
	"""
	rules = [
		asdict(
			FieldRule(
				source_field=_s(row.get("source_field")).strip(),
				target_field=_s(row.get("target_field")).strip(),
				transform=_s(row.get("transform")).strip(),
				transform_args=_s(row.get("transform_args")),
				formula=_s(row.get("formula")),
				default_value=_s(row.get("default_value")),
				skip_if_empty=bool(row.get("skip_if_empty")),
				target_table=_s(row.get("target_table")).strip(),
			)
		)
		for row in mapping.get("fields") or []
	]
	return {
		"schema_version": SNAPSHOT_VERSION,
		"mapping": _s(mapping.get("name") or mapping.get("mapping_name")),
		"connection": _s(mapping.get("connection")),
		"source_object": _s(mapping.get("source_object")),
		"target_doctype": _s(mapping.get("target_doctype")),
		"source_id_field": _s(mapping.get("source_id_field")).strip(),
		"mode": mode or _s(mapping.get("default_mode")) or "Insert",
		"match_field": _s(mapping.get("match_field")).strip(),
		"rules": rules,
	}


def spec_from_snapshot(snapshot: Mapping[str, Any]) -> MappingSpec:
	try:
		return MappingSpec(
			mapping=snapshot["mapping"],
			connection=snapshot["connection"],
			source_object=snapshot["source_object"],
			target_doctype=snapshot["target_doctype"],
			source_id_field=snapshot.get("source_id_field", ""),
			mode=snapshot["mode"],
			match_field=snapshot.get("match_field", ""),
			rules=tuple(FieldRule(**rule) for rule in snapshot["rules"]),
			schema_version=int(snapshot.get("schema_version", SNAPSHOT_VERSION)),
		)
	except (KeyError, TypeError) as exc:
		raise DataBridgeError(errors.MAPPING_INVALID, f"Mapping snapshot is malformed: {exc}") from None


def validate_rules(
	rules: Iterable[FieldRule],
	valid_targets: set[str],
	mode: str,
	match_field: str = "",
	source_id_required: bool = False,
	source_id_field: str = "",
) -> list[str]:
	"""Return a list of human-readable problems. Empty list means the mapping is usable."""
	problems: list[str] = []
	seen: set[str] = set()
	rules = list(rules)
	if not rules:
		problems.append("At least one field mapping is required")
	if mode not in MODES:
		problems.append(f"Mode must be one of {', '.join(MODES)}")
	if source_id_required and not source_id_field:
		problems.append("Source ID field is required for this connector")
	for index, rule in enumerate(rules, start=1):
		label = f"Row {index}"
		if rule.target_table:
			problems.append(f"{label}: child-table mapping is not supported yet")
		if not rule.source_field and not rule.default_value and rule.transform != FORMULA:
			problems.append(f"{label}: source field is required")
		if rule.target_field not in valid_targets:
			problems.append(f"{label}: target field {rule.target_field!r} does not exist")
		if rule.target_field in seen:
			problems.append(f"{label}: target field {rule.target_field!r} is mapped twice")
		seen.add(rule.target_field)
		if rule.transform and rule.transform not in TRANSFORM_NAMES:
			problems.append(f"{label}: unknown transform {rule.transform!r}")
		if rule.transform == FORMULA:
			problem = check_formula(rule.formula)
			if problem:
				problems.append(f"{label}: {problem}")
	if match_field and match_field not in valid_targets:
		problems.append(f"Match field {match_field!r} does not exist on the target")
	if mode == "Update" and not match_field:
		problems.append("Match field is required in Update mode")
	return problems


def extract_source_id(row: Mapping[str, Any], source_id_field: str) -> str:
	"""Stable string id for a source record. Missing or blank is a record-level error."""
	if not source_id_field:
		raise DataBridgeError(errors.MISSING_SOURCE_ID, "No source ID field is configured")
	value = row.get(source_id_field)
	if is_empty(value):
		raise DataBridgeError(errors.MISSING_SOURCE_ID, f"Source ID column {source_id_field!r} is empty")
	if isinstance(value, float) and value.is_integer():
		value = int(value)  # XLSX returns 5.0 for the number 5
	return str(value).strip()


def map_record(row: Mapping[str, Any], spec: MappingSpec, evaluator: Evaluator | None = None) -> MappedRecord:
	"""Turn one source row into target field values. Raises DataBridgeError on the first problem."""
	source_id = extract_source_id(row, spec.source_id_field)
	values: dict[str, Any] = {}
	for rule in spec.rules:
		raw = row.get(rule.source_field) if rule.source_field else None
		if is_empty(raw) and rule.default_value and rule.transform != FORMULA:
			raw = rule.default_value
		try:
			value = apply_transform(
				rule.transform,
				raw,
				rule.transform_args,
				formula=rule.formula,
				row=row,
				evaluator=evaluator,
			)
		except DataBridgeError as exc:
			raise DataBridgeError(exc.code, f"{rule.target_field}: {exc.message}") from None
		if is_empty(value):
			if rule.default_value and rule.transform == FORMULA:
				value = rule.default_value
			elif rule.skip_if_empty:
				continue
			else:
				value = None
		values[rule.target_field] = value
	return MappedRecord(source_id=source_id, values=values)
