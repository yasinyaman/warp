"""Database comment reader.

Reads TABLE and COLUMN comments through the dialect's catalog queries
(see :mod:`warp.adapters.outbound.db.dialect`) using the gateway's
``execute_query()``. Dialects without a comment catalog yield no comments.
"""

import logging

from warp.adapters.outbound.db.dialect import Dialect, dialect_or_generic
from warp.application.ports.database import SqlReader
from warp.domain.comments import TableComments

logger = logging.getLogger(__name__)


class CommentReader:
    """Reads database comments (TABLE and COLUMN) via warp adapter."""

    def __init__(
        self,
        adapter: SqlReader,
        db_type: str | Dialect = "postgresql",
        schema: str = "public",
        database: str = "",
    ):
        """Store the adapter and dialect/schema/database context."""
        self.adapter = adapter
        self.dialect = dialect_or_generic(db_type)
        self.db_type = self.dialect.name
        self.schema = schema
        self.database = database or schema
        self.queries = self.dialect.comment_queries

    def _scope_params(self) -> dict[str, str]:
        """The scope placeholder the dialect's queries expect."""
        if self.queries is not None and self.queries.scope_param == "database":
            return {"database": self.database}
        return {"schema": self.schema}

    async def read_table_comment(self, table_name: str) -> str | None:
        """Read comment for a single table."""
        if self.queries is None:
            logger.warning(f"Unsupported DB type for comments: {self.db_type}")
            return None
        try:
            rows = await self.adapter.execute_query(
                self.queries.table,
                {"table_name": table_name, **self._scope_params()},
            )
            if rows and rows[0].get("comment"):
                comment: str = rows[0]["comment"]
                return comment
            return None

        except Exception as e:
            logger.warning(f"Failed to read table comment for {table_name}: {e}")
            return None

    async def read_column_comments(self, table_name: str) -> dict[str, str]:
        """Read comments for all columns in a table."""
        if self.queries is None:
            return {}
        try:
            rows = await self.adapter.execute_query(
                self.queries.columns,
                {"table_name": table_name, **self._scope_params()},
            )
            return {row["column_name"]: row["comment"] for row in rows if row.get("comment")}

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

    async def read_all_comments(  # noqa: C901, PLR0912
        self, table_names: list[str] | None = None
    ) -> dict[str, TableComments]:
        """Read all comments for multiple tables efficiently."""
        result: dict[str, TableComments] = {}
        if self.queries is None:
            logger.warning(f"Unsupported DB type for comments: {self.db_type}")
            return result

        try:
            table_rows = await self.adapter.execute_query(
                self.queries.all_tables, self._scope_params()
            )
            column_rows = await self.adapter.execute_query(
                self.queries.all_columns, self._scope_params()
            )

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
