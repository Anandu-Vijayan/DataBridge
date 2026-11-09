from __future__ import annotations

import pytest

from databridge.engine import errors
from databridge.engine.control import TRANSITIONS, require_transition
from databridge.engine.errors import DataBridgeError
from databridge.engine.mapping import suggest_rules

ALL_STATUSES = [
	"Draft",
	"Queued",
	"Running",
	"Paused",
	"Cancelled",
	"Completed",
	"Completed With Errors",
	"Failed",
]


@pytest.mark.parametrize("action", sorted(TRANSITIONS))
@pytest.mark.parametrize("status", ALL_STATUSES)
def test_transition_matrix(action, status):
	if status in TRANSITIONS[action]:
		require_transition(action, status)
	else:
		with pytest.raises(DataBridgeError) as info:
			require_transition(action, status)
		assert info.value.code == errors.INVALID_STATE


def test_unknown_action():
	with pytest.raises(DataBridgeError) as info:
		require_transition("explode", "Draft")
	assert info.value.code == errors.INVALID_INPUT


def test_finished_jobs_cannot_restart_or_pause():
	for status in ("Completed", "Cancelled"):
		for action in ("start", "pause", "resume", "retry"):
			assert status not in TRANSITIONS[action]


def test_suggest_rules_exact_normalised_matches_only():
	targets = {"customer_name": "Customer Name", "mobile_no": "Mobile No", "name": "ID", "notes": "Notes"}
	rows = suggest_rules(["Customer Name", "MOBILE-NO", "customer_name", "unrelated", "Notes"], targets)
	assert rows == [
		{"source_field": "Customer Name", "target_field": "customer_name"},
		{"source_field": "MOBILE-NO", "target_field": "mobile_no"},
		{"source_field": "Notes", "target_field": "notes"},
	]


def test_suggest_rules_each_target_used_once_and_capped():
	rows = suggest_rules(["a", "A"], {"a": "A"})
	assert len(rows) == 1
	many = suggest_rules([f"f{i}" for i in range(10)], {f"f{i}": f"F{i}" for i in range(10)}, max_rows=3)
	assert len(many) == 3
