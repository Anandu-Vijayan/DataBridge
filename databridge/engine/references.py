"""Migration References: the idempotency ledger linking a source record to its target document.

A reference is always written in the same transaction as the target document, and it is always
checked before any write or retry. The key is a digest so it fits a unique index whatever the ids.
"""

from __future__ import annotations

import hashlib
from typing import Any

REFERENCE_DOCTYPE = "DataBridge Migration Reference"
_SEP = "\x1f"


def make_ref_key(connection: str, source_object: str, source_id: str, target_doctype: str) -> str:
	"""Stable 40-char digest of (connection, source object, source id, target DocType)."""
	raw = _SEP.join((connection, source_object, source_id, target_doctype))
	return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def find_reference(ref_key: str) -> dict[str, Any] | None:
	import frappe

	return frappe.db.get_value(
		REFERENCE_DOCTYPE, {"ref_key": ref_key}, ["name", "target_doctype", "target_name"], as_dict=True
	)


def save_reference(
	*,
	ref_key: str,
	existing: dict[str, Any] | None,
	connection: str,
	source_object: str,
	source_id: str,
	target_doctype: str,
	target_name: str,
	job: str,
) -> None:
	"""Create or repoint the reference. Runs inside the caller's transaction (no commit here)."""
	import frappe

	if existing:
		frappe.db.set_value(
			REFERENCE_DOCTYPE,
			existing["name"],
			{"target_name": target_name, "job": job},
			update_modified=False,
		)
		return
	# Bookkeeping DocType written by the worker as the run's user, who may not hold a role on it.
	# This never touches a target DocType, which always goes through normal permission checks.
	frappe.get_doc(
		{
			"doctype": REFERENCE_DOCTYPE,
			"ref_key": ref_key,
			"connection": connection,
			"source_object": source_object,
			"source_id": source_id,
			"target_doctype": target_doctype,
			"target_name": target_name,
			"job": job,
		}
	).insert(ignore_permissions=True)
