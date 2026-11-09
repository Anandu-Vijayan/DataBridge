"""Job control state machine (pure). The API layer asks here before changing a job's status."""

from __future__ import annotations

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

TRANSITIONS: dict[str, frozenset[str]] = {
	"start": frozenset({"Draft"}),
	"pause": frozenset({"Queued", "Running"}),
	"resume": frozenset({"Paused", "Failed"}),
	"cancel": frozenset({"Queued", "Running", "Paused"}),
	"retry": frozenset({"Completed With Errors", "Failed"}),
}


def require_transition(action: str, status: str) -> None:
	allowed = TRANSITIONS.get(action)
	if allowed is None:
		raise DataBridgeError(errors.INVALID_INPUT, f"Unknown action {action!r}")
	if status not in allowed:
		raise DataBridgeError(
			errors.INVALID_STATE,
			f"Cannot {action} a job that is {status}. Allowed from: {', '.join(sorted(allowed))}",
		)
