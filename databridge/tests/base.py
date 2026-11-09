"""Single place that decides which Frappe test base class the suite uses.

v15 ships FrappeTestCase (frappe.tests.utils); v16 replaces it with IntegrationTestCase.
Every integration test imports DataBridgeTestCase from here, so a version switch is a one-file change.
"""

from __future__ import annotations

try:  # v16
	from frappe.tests import IntegrationTestCase as _Base
except ImportError:  # v15
	from frappe.tests.utils import FrappeTestCase as _Base


class DataBridgeTestCase(_Base):
	"""Base class for integration tests that need a site."""
