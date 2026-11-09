"""Locate uploaded source files safely."""

from __future__ import annotations

import os

from databridge.engine import errors
from databridge.engine.errors import DataBridgeError


def resolve_private_file_path(file_url: str | None) -> str:
	"""Absolute path of a private attachment. Public files and paths outside the site are refused."""
	import frappe

	if not file_url:
		raise DataBridgeError(errors.SOURCE_FILE_MISSING, "No source file is attached to this connection")
	file_name = frappe.db.get_value("File", {"file_url": file_url}, "name")
	if not file_name:
		raise DataBridgeError(errors.SOURCE_FILE_MISSING, "The attached file no longer exists")
	file_doc = frappe.get_doc("File", file_name)
	if not file_doc.is_private:
		raise DataBridgeError(
			errors.INVALID_INPUT, "Source files must be private. Re-upload the file as private."
		)
	path = os.path.realpath(file_doc.get_full_path())
	base = os.path.realpath(frappe.get_site_path("private", "files"))
	if os.path.commonpath([path, base]) != base:
		raise DataBridgeError(errors.INVALID_INPUT, "The source file is outside the site's private folder")
	return path
