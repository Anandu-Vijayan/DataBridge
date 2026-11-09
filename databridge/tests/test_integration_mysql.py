"""Live MySQL/MariaDB source test. Skipped unless credentials are supplied through the environment.

    DATABRIDGE_TEST_MYSQL_HOST, DATABRIDGE_TEST_MYSQL_USER, DATABRIDGE_TEST_MYSQL_PASSWORD,
    DATABRIDGE_TEST_MYSQL_DB  (optional: DATABRIDGE_TEST_MYSQL_PORT, default 3306)

Never put these values in the repository.
"""

from __future__ import annotations

import os
import unittest

from databridge.connectors.base import ConnectionConfig
from databridge.connectors.mysql_connector import MySQLConnector
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError

ENV = {k: os.environ.get(f"DATABRIDGE_TEST_MYSQL_{k}") for k in ("HOST", "USER", "PASSWORD", "DB", "PORT")}
CONFIGURED = all(ENV[k] for k in ("HOST", "USER", "PASSWORD", "DB"))
TABLE = "databridge_it_people"


@unittest.skipUnless(CONFIGURED, "DATABRIDGE_TEST_MYSQL_* environment variables are not set")
class TestMySQLConnectorLive(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		import pymysql

		cls.config = ConnectionConfig(
			"MySQL",
			{
				"host": ENV["HOST"],
				"port": int(ENV["PORT"] or 3306),
				"username": ENV["USER"],
				"database": ENV["DB"],
			},
			password=ENV["PASSWORD"],
		)
		cls.admin = pymysql.connect(
			host=ENV["HOST"],
			port=int(ENV["PORT"] or 3306),
			user=ENV["USER"],
			password=ENV["PASSWORD"],
			database=ENV["DB"],
			autocommit=True,
		)
		with cls.admin.cursor() as cur:
			cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
			cur.execute(f"CREATE TABLE `{TABLE}` (id INT PRIMARY KEY, name VARCHAR(50), joined DATE)")
			cur.executemany(
				f"INSERT INTO `{TABLE}` VALUES (%s, %s, %s)",
				[(i, f"person {i}", f"2026-01-{i:02d}") for i in range(1, 8)],
			)

	@classmethod
	def tearDownClass(cls):
		with cls.admin.cursor() as cur:
			cur.execute(f"DROP TABLE IF EXISTS `{TABLE}`")
		cls.admin.close()

	def connector(self) -> MySQLConnector:
		return MySQLConnector(self.config)

	def test_connection_discovery_and_primary_key(self):
		c = self.connector()
		c.test()
		self.assertIn(TABLE, c.list_objects())
		self.assertEqual(c.list_fields(TABLE), ["id", "name", "joined"])
		self.assertEqual(c.default_id_columns(TABLE), ["id"])

	def test_keyset_pagination_reads_everything_once(self):
		c = self.connector()
		seen, cursor = [], None
		while True:
			batch = c.read_batch(TABLE, cursor, 3)
			seen.extend(r.data["id"] for r in batch.rows)
			cursor = batch.cursor
			if batch.done:
				break
		self.assertEqual(seen, list(range(1, 8)))

	def test_session_is_read_only(self):
		import pymysql

		c = self.connector()
		connection = c._connect()
		try:
			with self.assertRaises(pymysql.err.Error), connection.cursor() as cur:
				cur.execute(f"DELETE FROM `{TABLE}`")
		finally:
			connection.close()

	def test_unknown_table_is_rejected_before_any_sql_on_it(self):
		with self.assertRaises(DataBridgeError) as ctx:
			self.connector().read_batch(f"{TABLE}; DROP TABLE {TABLE}", None, 5)
		self.assertEqual(ctx.exception.code, errors.INVALID_IDENTIFIER)
		self.assertEqual(len(self.connector().read_batch(TABLE, None, 100).rows), 7)

	def test_wrong_password_error_is_redacted(self):
		bad = ConnectionConfig("MySQL", {**self.config.options}, password="definitely-wrong-pw")
		with self.assertRaises(DataBridgeError) as ctx:
			MySQLConnector(bad).test()
		self.assertEqual(ctx.exception.code, errors.CONNECTION_FAILED)
		self.assertNotIn("definitely-wrong-pw", ctx.exception.message)
