"""Dialect-aware SQL builder shared by the database adapters.

This is the single source of truth for constructing CRUD SQL. It centralizes the
two things that differ between PostgreSQL and MySQL — placeholder style
(``$1`` vs ``%s``), identifier quoting (``"x"`` vs `` `x` ``), the ``ILIKE`` vs
``LIKE`` operator, and ``RETURNING`` support — so the adapters only deal with
driver-specific *execution*.

Invariants:
- Values are ALWAYS emitted as bound-parameter placeholders, never interpolated.
- Identifiers are validated and quoted via :mod:`warp.adapters.outbound.db.identifiers`.
- Only integer pagination (already range-validated upstream) is interpolated.
"""

from typing import Any

from warp.adapters.outbound.db.identifiers import quote_identifier


class SafeQueryBuilder:
    """Builds parameterized SQL strings for a given SQL dialect."""

    def __init__(self, dialect: str) -> None:
        """Create a builder for the given dialect ("postgresql" or "mysql")."""
        self.dialect = dialect

    # --- dialect primitives -------------------------------------------------

    def _placeholder(self, index: int) -> str:
        """Positional placeholder: ``$N`` for PostgreSQL, ``%s`` for MySQL."""
        return f"${index}" if self.dialect == "postgresql" else "%s"

    def quote(self, name: str) -> str:
        """Validate and quote an identifier for this dialect."""
        return quote_identifier(name, self.dialect)

    @property
    def supports_returning(self) -> bool:
        """Whether this dialect supports a ``RETURNING`` clause."""
        return self.dialect == "postgresql"

    @property
    def _like_operator(self) -> str:
        return "ILIKE" if self.dialect == "postgresql" else "LIKE"

    def _select_columns(self, columns: list[str] | None) -> str:
        if columns:
            return ", ".join(self.quote(c) for c in columns)
        return "*"

    # --- WHERE --------------------------------------------------------------

    def where_clause(
        self, column: str, operator: str, value: Any, index: int
    ) -> tuple[str, int, list[Any]]:
        """Build a single WHERE condition.

        Returns ``(clause, next_index, params)``. ``next_index`` is the running
        placeholder index (used by PostgreSQL; harmless for MySQL).
        """
        col = self.quote(column)
        params: list[Any] = []

        simple_ops = {
            "eq": "=",
            "ne": "!=",
            "gt": ">",
            "gte": ">=",
            "lt": "<",
            "lte": "<=",
        }

        if operator in simple_ops:
            clause = f"{col} {simple_ops[operator]} {self._placeholder(index)}"
            params.append(value)
            index += 1
        elif operator == "like":
            clause = f"{col} {self._like_operator} {self._placeholder(index)}"
            params.append(value)
            index += 1
        elif operator == "in":
            if isinstance(value, list | tuple):
                placeholders = [self._placeholder(index + i) for i in range(len(value))]
                clause = f"{col} IN ({', '.join(placeholders)})"
                params.extend(value)
                index += len(value)
            else:
                clause = f"{col} = {self._placeholder(index)}"
                params.append(value)
                index += 1
        elif operator == "is_null":
            clause = f"{col} IS NULL" if value else f"{col} IS NOT NULL"
        else:
            clause = f"{col} = {self._placeholder(index)}"
            params.append(value)
            index += 1

        return clause, index, params

    def build_where(
        self, filters: list[tuple[str, str, Any]], start_index: int = 1
    ) -> tuple[str, int, list[Any]]:
        """Build the full ``WHERE ...`` clause (empty string when no filters)."""
        clauses: list[str] = []
        params: list[Any] = []
        index = start_index
        for column, operator, value in filters:
            clause, index, new_params = self.where_clause(column, operator, value, index)
            clauses.append(clause)
            params.extend(new_params)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_sql, index, params

    def _order_by(self, sort: list[tuple[str, str]] | None) -> str:
        if not sort:
            return ""
        parts = [f"{self.quote(col)} {direction.upper()}" for col, direction in sort]
        return f"ORDER BY {', '.join(parts)}"

    @staticmethod
    def _limit_offset(pagination: dict[str, int] | None) -> str:
        if not pagination:
            return ""
        limit = pagination.get("limit", 50)
        offset = pagination.get("offset", 0)
        return f"LIMIT {limit} OFFSET {offset}"

    # --- statements ---------------------------------------------------------

    def build_select(
        self,
        table: str,
        columns: list[str] | None,
        filters: list[tuple[str, str, Any]] | None,
        pagination: dict[str, int] | None,
        sort: list[tuple[str, str]] | None,
    ) -> tuple[str, str, list[Any]]:
        """Return ``(count_sql, select_sql, params)`` for a list query."""
        tbl = self.quote(table)
        cols = self._select_columns(columns)
        where_sql, _, params = self.build_where(filters or [])
        order_sql = self._order_by(sort)
        limit_sql = self._limit_offset(pagination)

        count_sql = f"SELECT COUNT(*) AS cnt FROM {tbl} {where_sql}".rstrip()
        select_sql = f"SELECT {cols} FROM {tbl} {where_sql} {order_sql} {limit_sql}".rstrip()
        return count_sql, select_sql, params

    def build_insert(self, table: str, data: dict[str, Any]) -> tuple[str, list[Any]]:
        """Build an INSERT statement (with ``RETURNING *`` on PostgreSQL)."""
        tbl = self.quote(table)
        columns = list(data.keys())
        quoted_cols = ", ".join(self.quote(c) for c in columns)
        placeholders = ", ".join(self._placeholder(i + 1) for i in range(len(columns)))
        returning = " RETURNING *" if self.supports_returning else ""
        sql = f"INSERT INTO {tbl} ({quoted_cols}) VALUES ({placeholders}){returning}"
        return sql, list(data.values())

    def build_select_by_id(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        columns: list[str] | None,
    ) -> tuple[str, list[Any]]:
        """Build a single-row SELECT by primary key."""
        cols = self._select_columns(columns)
        sql = (
            f"SELECT {cols} FROM {self.quote(table)} "
            f"WHERE {self.quote(id_column)} = {self._placeholder(1)}"
        )
        return sql, [id_value]

    def build_update(
        self,
        table: str,
        id_column: str,
        id_value: Any,
        data: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        """Build an UPDATE-by-id statement (``RETURNING *`` on PostgreSQL)."""
        tbl = self.quote(table)
        set_parts: list[str] = []
        params: list[Any] = []
        index = 1
        for column, value in data.items():
            set_parts.append(f"{self.quote(column)} = {self._placeholder(index)}")
            params.append(value)
            index += 1
        where = f"{self.quote(id_column)} = {self._placeholder(index)}"
        params.append(id_value)
        returning = " RETURNING *" if self.supports_returning else ""
        sql = f"UPDATE {tbl} SET {', '.join(set_parts)} WHERE {where}{returning}"
        return sql, params

    def build_delete(self, table: str, id_column: str, id_value: Any) -> tuple[str, list[Any]]:
        """Build a DELETE-by-id statement (``RETURNING`` id on PostgreSQL)."""
        col = self.quote(id_column)
        returning = f" RETURNING {col}" if self.supports_returning else ""
        sql = f"DELETE FROM {self.quote(table)} WHERE {col} = {self._placeholder(1)}{returning}"
        return sql, [id_value]
