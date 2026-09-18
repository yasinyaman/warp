"""MySQL database adapter implementation."""

from collections.abc import AsyncIterator
from typing import Any

import aiomysql
from pymysql.constants import CLIENT

from warp.adapters.outbound.db.base import DatabaseAdapter
from warp.adapters.outbound.db.dialect import MYSQL
from warp.adapters.outbound.db.identifiers import sanitize_identifier
from warp.adapters.outbound.db.params import bind_named_params
from warp.adapters.outbound.db.query_builder import SafeQueryBuilder


class MySQLAdapter(DatabaseAdapter):
    """MySQL database adapter using aiomysql."""

    _qb = SafeQueryBuilder(MYSQL)

    async def connect(self) -> None:
        """Create connection pool to MySQL."""
        options = self.config.get("options", {})

        self._pool = await aiomysql.create_pool(
            host=self.config["host"],
            port=self.config["port"],
            db=self.config["database"],
            user=self.config["username"],
            password=self.config["password"],
            minsize=options.get("pool_min_size", 2),
            maxsize=options.get("pool_size", 10),
            autocommit=True,
            # Make cursor.rowcount report *matched* rows for UPDATE (MySQL's
            # default counts only *changed* rows), so updating a record with
            # its current values is not mistaken for "record not found".
            client_flag=CLIENT.FOUND_ROWS,
        )

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def get_tables(self) -> list[str]:
        """Get all table names from the database."""
        # Result-column names follow the SELECT list on MariaDB/MySQL 5.7 but
        # are always upper-cased by MySQL 8 for information_schema; selecting
        # the upper-case name keeps the key identical on every server.
        query = """
            SELECT TABLE_NAME
            FROM information_schema.tables
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
        """
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, (self.config["database"],))
            rows = await cur.fetchall()
            return [row["TABLE_NAME"] for row in rows]

    async def get_table_schema(self, table: str) -> dict[str, Any]:
        """Get detailed schema information for a table."""
        schema: dict[str, Any] = {
            "table_name": table,
            "columns": [],
            "primary_key": None,
            "foreign_keys": [],
            "indexes": [],
        }

        db_name = self.config["database"]

        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            # Get column information
            columns_query = """
                    SELECT
                        COLUMN_NAME,
                        DATA_TYPE,
                        COLUMN_TYPE,
                        IS_NULLABLE,
                        COLUMN_DEFAULT,
                        CHARACTER_MAXIMUM_LENGTH,
                        NUMERIC_PRECISION,
                        NUMERIC_SCALE,
                        COLUMN_KEY,
                        EXTRA
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                    ORDER BY ORDINAL_POSITION
                """
            await cur.execute(columns_query, (db_name, table))
            columns = await cur.fetchall()

            for col in columns:
                schema["columns"].append(
                    {
                        "name": col["COLUMN_NAME"],
                        "type": col["DATA_TYPE"],
                        "full_type": col["COLUMN_TYPE"],
                        "nullable": col["IS_NULLABLE"] == "YES",
                        "default": col["COLUMN_DEFAULT"],
                        "max_length": col["CHARACTER_MAXIMUM_LENGTH"],
                        "precision": col["NUMERIC_PRECISION"],
                        "scale": col["NUMERIC_SCALE"],
                        "key": col["COLUMN_KEY"],
                        "extra": col["EXTRA"],
                    }
                )

                # Check for primary key
                if col["COLUMN_KEY"] == "PRI":
                    if schema["primary_key"] is None:
                        schema["primary_key"] = col["COLUMN_NAME"]
                    elif isinstance(schema["primary_key"], str):
                        schema["primary_key"] = [schema["primary_key"], col["COLUMN_NAME"]]
                    else:
                        schema["primary_key"].append(col["COLUMN_NAME"])

            # Get foreign keys
            fk_query = """
                    SELECT
                        COLUMN_NAME,
                        REFERENCED_TABLE_NAME,
                        REFERENCED_COLUMN_NAME,
                        CONSTRAINT_NAME
                    FROM information_schema.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                """
            await cur.execute(fk_query, (db_name, table))
            fks = await cur.fetchall()

            for fk in fks:
                schema["foreign_keys"].append(
                    {
                        "column": fk["COLUMN_NAME"],
                        "references_table": fk["REFERENCED_TABLE_NAME"],
                        "references_column": fk["REFERENCED_COLUMN_NAME"],
                        "constraint_name": fk["CONSTRAINT_NAME"],
                    }
                )

            # Get indexes
            idx_query = """
                    SELECT
                        INDEX_NAME,
                        COLUMN_NAME,
                        NON_UNIQUE,
                        SEQ_IN_INDEX
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = %s
                      AND TABLE_NAME = %s
                      AND INDEX_NAME != 'PRIMARY'
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX
                """
            await cur.execute(idx_query, (db_name, table))
            indexes = await cur.fetchall()

            # Group index columns
            idx_map = {}
            for idx in indexes:
                name = idx["INDEX_NAME"]
                if name not in idx_map:
                    idx_map[name] = {"name": name, "columns": [], "unique": idx["NON_UNIQUE"] == 0}
                idx_map[name]["columns"].append(idx["COLUMN_NAME"])

            schema["indexes"] = list(idx_map.values())

        return schema

    async def row_estimates(self, tables: list[str]) -> dict[str, int | None]:
        """Estimates from ``information_schema.TABLES.TABLE_ROWS`` (NULL = unknown)."""
        if not tables:
            return {}
        placeholders = ", ".join("%s" for _ in tables)
        query = f"""
            SELECT TABLE_NAME, TABLE_ROWS
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME IN ({placeholders})
        """  # noqa: S608 - only placeholders are interpolated
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, (self.config["database"], *tables))
            rows = await cur.fetchall()
        found = {
            row["TABLE_NAME"]: (None if row["TABLE_ROWS"] is None else int(row["TABLE_ROWS"]))
            for row in rows
        }
        return {table: found.get(table) for table in tables}

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a raw SQL query."""
        sql, args = bind_named_params(query, params, self._qb.dialect)
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            if args:
                await cur.execute(sql, args)
            else:
                await cur.execute(sql)

            rows = await cur.fetchall()
            return list(rows)

    async def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a new record."""
        query, values = self._qb.build_insert(table, data)

        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, values)
            last_id = cur.lastrowid

            # Fetch the inserted record (assumes an auto-increment `id` PK)
            if last_id:
                refetch, refetch_params = self._qb.build_select_by_id(table, "id", last_id, None)
                await cur.execute(refetch, refetch_params)
                row = await cur.fetchone()
                return row if row else data
            return data

    async def select(
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        pagination: dict[str, int] | None = None,
        sort: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Select records with filtering, pagination, and sorting."""
        count_query, query, params = self._qb.build_select(
            table, columns, filters, pagination, sort
        )
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(count_query, params)
            count_row = await cur.fetchone()
            total = count_row["cnt"] if count_row else 0

            await cur.execute(query, params)
            rows = await cur.fetchall()
            return list(rows), total

    async def stream_select(  # noqa: PLR0913
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        sort: list[tuple[str, str]] | None = None,
        batch_size: int = 5000,
        limit: int | None = None,
        statement_timeout_ms: int = 0,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Stream rows through an unbuffered (server-side) dict cursor."""
        sql, params = self._qb.build_stream_select(table, columns, filters, sort, limit)
        if statement_timeout_ms > 0:
            # Per-statement optimizer hint (MySQL >= 5.7.8); MariaDB ignores it.
            sql = sql.replace(
                "SELECT ", f"SELECT /*+ MAX_EXECUTION_TIME({int(statement_timeout_ms)}) */ ", 1
            )
        # The `async with` closes the unbuffered cursor (draining it) before the
        # connection goes back to the pool, even when the consumer stops early.
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.SSDictCursor) as cur:
            await cur.execute(sql, params or None)
            while True:
                rows = await cur.fetchmany(batch_size)
                if not rows:
                    break
                yield list(rows)

    async def select_by_id(
        self, table: str, id_column: str, id_value: Any, columns: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Select a single record by ID."""
        query, params = self._qb.build_select_by_id(table, id_column, id_value, columns)
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, params)
            row: dict[str, Any] | None = await cur.fetchone()
            return row

    async def update(
        self, table: str, id_column: str, id_value: Any, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an existing record."""
        if not data:
            return await self.select_by_id(table, id_column, id_value)

        query, values = self._qb.build_update(table, id_column, id_value, data)
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, values)

            # rowcount == matched rows thanks to CLIENT.FOUND_ROWS (see connect):
            # 0 means no such record, not "nothing changed".
            if cur.rowcount > 0:
                return await self.select_by_id(table, id_column, id_value)
            return None

    async def delete(self, table: str, id_column: str, id_value: Any) -> bool:
        """Delete a record by ID."""
        query, params = self._qb.build_delete(table, id_column, id_value)
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(query, params)
            return bool(cur.rowcount > 0)

    def _sanitize_identifier(self, name: str) -> str:
        """Validate a SQL identifier (delegates to the shared sanitizer)."""
        return sanitize_identifier(name)

    def _build_where_clause(self, column: str, operator: str, value: Any) -> tuple[str, list[Any]]:
        """Build a WHERE clause component (delegates to the shared builder)."""
        clause, _, params = self._qb.where_clause(column, operator, value, 1)
        return clause, params
