"""Database comment reader.

Reads TABLE and COLUMN comments from PostgreSQL and MySQL databases
using warp's DatabaseAdapter.execute_query() method.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from warp.core.logging import get_logger

logger = get_logger(__name__)


class DatabaseAdapterProtocol(Protocol):
    """Protocol matching warp's DatabaseAdapter interface."""

    async def execute_query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]: ...


@dataclass
class TableComments:
    """Comments for a single table."""

    table_name: str
    table_comment: str | None = None
    column_comments: dict[str, str] = field(default_factory=dict)


# SQL queries for different database types

PG_TABLE_COMMENT_SQL = """
SELECT obj_description(c.oid) as comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = $1 AND n.nspname = $2
"""

PG_COLUMN_COMMENTS_SQL = """
SELECT a.attname as column_name, col_description(a.attrelid, a.attnum) as comment
FROM pg_attribute a
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = $1
  AND n.nspname = $2
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND col_description(a.attrelid, a.attnum) IS NOT NULL
"""

PG_ALL_TABLE_COMMENTS_SQL = """
SELECT c.relname as table_name, obj_description(c.oid) as comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1
  AND c.relkind = 'r'
  AND obj_description(c.oid) IS NOT NULL
"""

PG_ALL_COLUMN_COMMENTS_SQL = """
SELECT c.relname as table_name, a.attname as column_name,
       col_description(a.attrelid, a.attnum) as comment
FROM pg_attribute a
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND col_description(a.attrelid, a.attnum) IS NOT NULL
"""

MYSQL_TABLE_COMMENT_SQL = """
SELECT TABLE_COMMENT as comment
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_COMMENT != ''
"""

MYSQL_COLUMN_COMMENTS_SQL = """
SELECT COLUMN_NAME as column_name, COLUMN_COMMENT as comment
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_COMMENT != ''
"""

MYSQL_ALL_TABLE_COMMENTS_SQL = """
SELECT TABLE_NAME as table_name, TABLE_COMMENT as comment
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_COMMENT != ''
"""

MYSQL_ALL_COLUMN_COMMENTS_SQL = """
SELECT TABLE_NAME as table_name, COLUMN_NAME as column_name, COLUMN_COMMENT as comment
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND COLUMN_COMMENT != ''
"""


class CommentReader:
    """Reads database comments (TABLE and COLUMN) via warp adapter."""

    def __init__(
        self,
        adapter: DatabaseAdapterProtocol,
        db_type: str = "postgresql",
        schema: str = "public",
        database: str = "",
    ):
        self.adapter = adapter
        self.db_type = db_type.lower()
        self.schema = schema
        self.database = database or schema

    async def read_table_comment(self, table_name: str) -> str | None:
        """Read comment for a single table."""
        try:
            if self.db_type == "postgresql":
                rows = await self.adapter.execute_query(
                    PG_TABLE_COMMENT_SQL,
                    {"p1": table_name, "p2": self.schema},
                )
            elif self.db_type == "mysql":
                rows = await self.adapter.execute_query(
                    MYSQL_TABLE_COMMENT_SQL,
                    {"p1": self.database, "p2": table_name},
                )
            else:
                logger.warning(f"Unsupported DB type for comments: {self.db_type}")
                return None

            if rows and rows[0].get("comment"):
                comment: str = rows[0]["comment"]
                return comment
            return None

        except Exception as e:
            logger.warning(f"Failed to read table comment for {table_name}: {e}")
            return None

    async def read_column_comments(self, table_name: str) -> dict[str, str]:
        """Read comments for all columns in a table."""
        try:
            if self.db_type == "postgresql":
                rows = await self.adapter.execute_query(
                    PG_COLUMN_COMMENTS_SQL,
                    {"p1": table_name, "p2": self.schema},
                )
            elif self.db_type == "mysql":
                rows = await self.adapter.execute_query(
                    MYSQL_COLUMN_COMMENTS_SQL,
                    {"p1": self.database, "p2": table_name},
                )
            else:
                return {}

            return {
                row["column_name"]: row["comment"]
                for row in rows
                if row.get("comment")
            }

        except Exception as e:
            logger.warning(f"Failed to read column comments for {table_name}: {e}")
            return {}

    async def read_table_comments(self, table_name: str) -> TableComments:
        """Read all comments (table + columns) for a single table."""
        table_comment = await self.read_table_comment(table_name)
        column_comments = await self.read_column_comments(table_name)

        return TableComments(
            table_name=table_name,
            table_comment=table_comment,
            column_comments=column_comments,
        )

    async def read_all_comments(
        self, table_names: list[str] | None = None
    ) -> dict[str, TableComments]:
        """Read all comments for multiple tables efficiently."""
        result: dict[str, TableComments] = {}

        try:
            if self.db_type == "postgresql":
                table_rows = await self.adapter.execute_query(
                    PG_ALL_TABLE_COMMENTS_SQL, {"p1": self.schema},
                )
                column_rows = await self.adapter.execute_query(
                    PG_ALL_COLUMN_COMMENTS_SQL, {"p1": self.schema},
                )
            elif self.db_type == "mysql":
                table_rows = await self.adapter.execute_query(
                    MYSQL_ALL_TABLE_COMMENTS_SQL, {"p1": self.database},
                )
                column_rows = await self.adapter.execute_query(
                    MYSQL_ALL_COLUMN_COMMENTS_SQL, {"p1": self.database},
                )
            else:
                logger.warning(f"Unsupported DB type for comments: {self.db_type}")
                return result

            table_comment_map: dict[str, str] = {}
            for row in table_rows:
                tname = row["table_name"]
                if table_names and tname not in table_names:
                    continue
                if row.get("comment"):
                    table_comment_map[tname] = row["comment"]

            column_comment_map: dict[str, dict[str, str]] = {}
            for row in column_rows:
                tname = row["table_name"]
                if table_names and tname not in table_names:
                    continue
                if row.get("comment"):
                    if tname not in column_comment_map:
                        column_comment_map[tname] = {}
                    column_comment_map[tname][row["column_name"]] = row["comment"]

            all_tables = set(table_comment_map.keys()) | set(column_comment_map.keys())
            if table_names:
                all_tables = all_tables & set(table_names)

            for tname in all_tables:
                result[tname] = TableComments(
                    table_name=tname,
                    table_comment=table_comment_map.get(tname),
                    column_comments=column_comment_map.get(tname, {}),
                )

            logger.info(
                f"Read database comments: {len(result)} tables, "
                f"{sum(len(tc.column_comments) for tc in result.values())} columns"
            )

        except Exception as e:
            logger.warning(f"Batch comment read failed, falling back to per-table: {e}")
            if table_names:
                for tname in table_names:
                    tc = await self.read_table_comments(tname)
                    if tc.table_comment or tc.column_comments:
                        result[tname] = tc

        return result
