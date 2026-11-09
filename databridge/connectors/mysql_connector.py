"""MySQL / MariaDB source. Read-only session, keyset pagination, validated identifiers.

Table and column names never come straight from user input into SQL: they are matched against what
information_schema reports and then backtick-quoted. Every value is a bound parameter.
"""

from __future__ import annotations

from typing import Any

from databridge.connectors.base import Batch, Connector, SourceRow, to_jsonable
from databridge.engine import errors
from databridge.engine.errors import DataBridgeError
from databridge.utils.redact import redact_text


def quote_identifier(name: str) -> str:
	"""Backtick-quote an identifier that has already been validated against the schema."""
	if not name or "\x00" in name:
		raise DataBridgeError(errors.INVALID_IDENTIFIER, "Invalid identifier")
	return "`" + name.replace("`", "``") + "`"


class MySQLConnector(Connector):
	name = "MySQL"

	# ------------------------------------------------------------------ connection
	def _connect(self):
		import pymysql

		opts = self.config.options
		kwargs: dict[str, Any] = {
			"host": opts.get("host"),
			"port": int(opts.get("port") or 3306),
			"user": opts.get("username"),
			"password": self.config.password or "",
			"database": opts.get("database"),
			"connect_timeout": 10,
			"read_timeout": 120,
			"charset": "utf8mb4",
			"autocommit": True,
		}
		if opts.get("use_ssl"):
			# Encrypts the link. Certificates are verified only when ssl_ca is given in extra options.
			kwargs["ssl"] = {"ca": opts.get("ssl_ca")} if opts.get("ssl_ca") else {"check_hostname": False}
		try:
			connection = pymysql.connect(**kwargs)
			with connection.cursor() as cur:
				cur.execute("SET SESSION TRANSACTION READ ONLY")
				cur.execute("SELECT @@session.transaction_read_only")
				if int(cur.fetchone()[0]) != 1:
					connection.close()
					raise DataBridgeError(
						errors.CONNECTION_FAILED, "Could not switch the source session to read-only"
					)
			return connection
		except DataBridgeError:
			raise
		except Exception as exc:
			message = redact_text(f"{type(exc).__name__}: {exc}", self.config.secrets())
			raise DataBridgeError(
				errors.CONNECTION_FAILED, f"Could not connect to the source database: {message}"
			) from None

	def _query(self, connection, sql: str, params: tuple = ()) -> list[tuple]:
		try:
			with connection.cursor() as cur:
				cur.execute(sql, params)
				return list(cur.fetchall())
		except DataBridgeError:
			raise
		except Exception as exc:
			message = redact_text(f"{type(exc).__name__}: {exc}", self.config.secrets())
			raise DataBridgeError(errors.SOURCE_READ_ERROR, f"Source query failed: {message}") from None

	# ------------------------------------------------------------------ schema discovery
	def _tables(self, connection) -> list[str]:
		rows = self._query(
			connection,
			"SELECT TABLE_NAME FROM information_schema.TABLES "
			"WHERE TABLE_SCHEMA = %s AND TABLE_TYPE IN ('BASE TABLE', 'VIEW') ORDER BY TABLE_NAME",
			(self.config.options.get("database"),),
		)
		return [r[0] for r in rows]

	def _columns(self, connection, table: str) -> tuple[list[str], list[str]]:
		"""Return (all columns, primary-key columns in key order) for a validated table."""
		rows = self._query(
			connection,
			"SELECT COLUMN_NAME, COLUMN_KEY FROM information_schema.COLUMNS "
			"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
			(self.config.options.get("database"), table),
		)
		columns = [r[0] for r in rows]
		pk = [r[0] for r in rows if r[1] == "PRI"]
		return columns, pk

	def _validated_table(self, connection, source_object: str) -> str:
		if source_object not in self._tables(connection):
			raise DataBridgeError(
				errors.INVALID_IDENTIFIER, f"Table {source_object!r} does not exist in the source database"
			)
		return source_object

	# ------------------------------------------------------------------ Connector API
	def test(self) -> None:
		connection = self._connect()
		try:
			self._query(connection, "SELECT 1")
		finally:
			connection.close()

	def list_objects(self) -> list[str]:
		connection = self._connect()
		try:
			return self._tables(connection)
		finally:
			connection.close()

	def list_fields(self, source_object: str) -> list[str]:
		connection = self._connect()
		try:
			return self._columns(connection, self._validated_table(connection, source_object))[0]
		finally:
			connection.close()

	def default_id_columns(self, source_object: str) -> list[str]:
		connection = self._connect()
		try:
			return self._columns(connection, self._validated_table(connection, source_object))[1]
		finally:
			connection.close()

	def build_select(
		self, table: str, columns: list[str], pk: list[str], cursor: dict[str, Any] | None, batch_size: int
	) -> tuple[str, tuple]:
		"""Build the paged SELECT. Only schema-validated identifiers are interpolated."""
		select_list = ", ".join(quote_identifier(c) for c in columns)
		sql = f"SELECT {select_list} FROM {quote_identifier(table)}"
		params: list[Any] = []
		if pk:
			pk_list = ", ".join(quote_identifier(c) for c in pk)
			if cursor and cursor.get("pk") is not None:
				marks = ", ".join(["%s"] * len(pk))
				sql += f" WHERE ({pk_list}) > ({marks})"
				params.extend(cursor["pk"])
			sql += f" ORDER BY {pk_list} LIMIT %s"
			params.append(batch_size)
		else:
			# No primary key: stable order over every column, offset paging.
			sql += f" ORDER BY {select_list} LIMIT %s OFFSET %s"
			params.extend([batch_size, int(cursor["offset"]) if cursor else 0])
		return sql, tuple(params)

	def read_batch(self, source_object: str, cursor: dict[str, Any] | None, batch_size: int) -> Batch:
		connection = self._connect()
		try:
			table = self._validated_table(connection, source_object)
			columns, pk = self._columns(connection, table)
			sql, params = self.build_select(table, columns, pk, cursor, batch_size)
			fetched = self._query(connection, sql, params)
		finally:
			connection.close()

		position = int(cursor["row"]) if cursor else 0
		rows: list[SourceRow] = []
		for values in fetched:
			position += 1
			rows.append(SourceRow(dict(zip(columns, values, strict=True)), position))

		next_cursor: dict[str, Any] = {"row": position}
		if pk and fetched:
			last = rows[-1].data
			next_cursor["pk"] = [to_jsonable(last[c]) for c in pk]
		elif pk and cursor:
			next_cursor["pk"] = cursor.get("pk")
		elif not pk:
			next_cursor["offset"] = (int(cursor["offset"]) if cursor else 0) + len(fetched)
		return Batch(rows=rows, cursor=next_cursor, done=len(fetched) < batch_size)
