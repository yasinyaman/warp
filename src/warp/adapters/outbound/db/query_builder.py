"""Dialect-aware SQL builder shared by the database adapters.

This is the single source of truth for constructing CRUD SQL. Everything that
differs between engines — placeholder style (``$1`` / ``%s`` / ``?``),
identifier quoting, ``ILIKE`` vs ``LIKE``, how written rows are handed back
(``RETURNING`` / ``OUTPUT`` / re-select) and pagination syntax — comes from the
:class:`~warp.adapters.outbound.db.dialect.Dialect`, so the adapters only deal
with driver-specific *execution*.

Invariants:
- Values are ALWAYS emitted as bound-parameter placeholders, never interpolated.
- Identifiers are validated and quoted via :mod:`warp.adapters.outbound.db.identifiers`.
- Only integer pagination (already range-validated upstream) is interpolated.
"""

from typing import Any

from warp.adapters.outbound.db.dialect import Dialect, ReturningStyle, get_dialect


class SafeQueryBuilder:
    """Builds parameterized SQL strings for a given SQL dialect."""

    def __init__(self, dialect: str | Dialect) -> None:
        """Create a builder for a dialect name (``postgresql``, ``mysql``, ``mssql``, ...) or object."""
        self.dialect: Dialect = get_dialect(dialect)

    # --- dialect primitives -------------------------------------------------

    def _placeholder(self, index: int) -> str:
        """Positional placeholder: ``$N``, ``%s`` or ``?`` depending on the dialect."""
        return self.dialect.placeholder(index)

    def quote(self, name: str) -> str:
        """Validate and quote an identifier for this dialect."""
        return self.dialect.quote(name)

    @property
    def returning_style(self) -> ReturningStyle:
        """How INSERT/UPDATE/DELETE hand back the affected row."""
        return self.dialect.returning_style

    @property
    def supports_returning(self) -> bool:
        """Whether a write statement can return the affected row by itself."""
        return self.dialect.returning_style != "refetch"

    @property
    def _like_operator(self) -> str:
        return self.dialect.like_operator

    def _select_columns(self, columns: list[str] | None) -> str:
        if columns:
            return ", ".join(self.quote(c) for c in columns)
        return "*"

    def _output(self, returning: bool, expression: str) -> str:
        """SQL Server ``OUTPUT`` clause (placed before ``VALUES``/``WHERE``)."""
        if returning and self.returning_style == "output":
            return f" OUTPUT {expression}"
        return ""

    def _returning(self, returning: bool, expression: str) -> str:
        """PostgreSQL ``RETURNING`` clause (placed at the end of the statement)."""
        if returning and self.returning_style == "returning":
            return f" RETURNING {expression}"
        return ""

    # --- WHERE --------------------------------------------------------------

    def where_clause(
        self, column: str, operator: str, value: Any, index: int
    ) -> tuple[str, int, list[Any]]:
        """Build a single WHERE condition.

        Returns ``(clause, next_index, params)``. ``next_index`` is the running
        placeholder index (used by numbered dialects; harmless for the others).
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

    def _order_and_limit(
        self, sort: list[tuple[str, str]] | None, pagination: dict[str, int] | None
    ) -> str:
        """``ORDER BY`` plus the dialect's pagination tail (may be empty)."""
        order_sql = self._order_by(sort)
        if not pagination:
            return self.dialect.order_and_limit(order_sql, None)
        return self.dialect.order_and_limit(
            order_sql, pagination.get("limit", 50), pagination.get("offset", 0)
        )

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
        tail = self._order_and_limit(sort, pagination)

        count_sql = " ".join(p for p in (f"SELECT COUNT(*) AS cnt FROM {tbl}", where_sql) if p)
        select_sql = " ".join(p for p in (f"SELECT {cols} FROM {tbl}", where_sql, tail) if p)
        return count_sql, select_sql, params

    def build_stream_select(
        self,
        table: str,
        columns: list[str] | None,
        filters: list[tuple[str, str, Any]] | None,
        sort: list[tuple[str, str]] | None,
        limit: int | None = None,
    ) -> tuple[str, list[Any]]:
        """Return ``(select_sql, params)`` for a streamed read: no COUNT, no paging.

        The row cap goes through the dialect, so it is ``LIMIT`` on
        PostgreSQL/MySQL and ``OFFSET ... FETCH`` on SQL Server; identifiers
        and values take the same validated paths as ``build_select``.
        """
        where_sql, _, params = self.build_where(filters or [])
        parts = [
            f"SELECT {self._select_columns(columns)} FROM {self.quote(table)}",
            where_sql,
            self.dialect.order_and_limit(self._order_by(sort), limit),
        ]
        return " ".join(part for part in parts if part), params

    def build_insert(
        self, table: str, data: dict[str, Any], *, returning: bool = True
    ) -> tuple[str, list[Any]]:
        """Build an INSERT statement (``RETURNING *`` / ``OUTPUT INSERTED.*`` when supported)."""
        tbl = self.quote(table)
        columns = list(data.keys())
        quoted_cols = ", ".join(self.quote(c) for c in columns)
        placeholders = ", ".join(self._placeholder(i + 1) for i in range(len(columns)))
        sql = (
            f"INSERT INTO {tbl} ({quoted_cols}){self._output(returning, 'INSERTED.*')} "
            f"VALUES ({placeholders}){self._returning(returning, '*')}"
        )
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
        *,
        returning: bool = True,
    ) -> tuple[str, list[Any]]:
        """Build an UPDATE-by-id statement (``RETURNING *`` / ``OUTPUT INSERTED.*`` when supported)."""
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
        sql = (
            f"UPDATE {tbl} SET {', '.join(set_parts)}{self._output(returning, 'INSERTED.*')} "
            f"WHERE {where}{self._returning(returning, '*')}"
        )
        return sql, params

    def build_delete(
        self, table: str, id_column: str, id_value: Any, *, returning: bool = True
    ) -> tuple[str, list[Any]]:
        """Build a DELETE-by-id statement (returning the id when supported)."""
        col = self.quote(id_column)
        sql = (
            f"DELETE FROM {self.quote(table)}{self._output(returning, f'DELETED.{col}')} "
            f"WHERE {col} = {self._placeholder(1)}{self._returning(returning, col)}"
        )
        return sql, [id_value]
