"""SQL dialect descriptors shared by the database adapters and SQL helpers.

A :class:`Dialect` captures everything that differs between the supported SQL
engines — placeholder style, identifier quoting, the case-insensitive ``LIKE``
operator, how written rows are returned, pagination syntax, the default schema
and the catalog (comment / row-count) queries — so that the query builder, the
parameter binder and the metadata readers never branch on a database-type
string themselves.

Adding an engine means adding one :class:`Dialect` instance to :data:`DIALECTS`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from warp.adapters.outbound.db.identifiers import sanitize_identifier

logger = logging.getLogger(__name__)

PlaceholderStyle = Literal["numbered", "format", "qmark"]
ReturningStyle = Literal["returning", "output", "refetch"]
LimitStyle = Literal["limit_offset", "offset_fetch"]


@dataclass(frozen=True)
class CommentQueries:
    """Catalog queries that read table and column comments.

    ``table``/``columns`` take ``:table_name`` plus the scope placeholder named
    by ``scope_param`` (``:schema`` or ``:database``); the ``all_*`` variants
    take only the scope placeholder and return a ``table_name`` column.
    """

    table: str
    columns: str
    all_tables: str
    all_columns: str
    scope_param: Literal["schema", "database"] = "schema"


@dataclass(frozen=True)
class Dialect:
    """Everything that differs between SQL engines, as data.

    Attributes:
        name: Canonical name (``postgresql``, ``mysql``, ``mssql``, ``odbc``).
        placeholder_style: ``numbered`` (``$1``), ``format`` (``%s``) or
            ``qmark`` (``?``).
        quote_chars: Opening/closing identifier quote characters.
        like_operator: Case-insensitive pattern operator (``ILIKE`` or ``LIKE``).
        returning_style: How INSERT/UPDATE/DELETE hand back the affected row:
            ``returning`` (``RETURNING ...``), ``output`` (SQL Server
            ``OUTPUT INSERTED.* / DELETED.x``) or ``refetch`` (no clause; the
            adapter re-selects the row).
        limit_style: ``limit_offset`` or SQL Server's ``offset_fetch``.
        escape_percent: Whether a literal ``%`` must be doubled when the driver
            applies printf-style formatting (aiomysql).
        schema_qualified: Whether table references are ``schema.table``.
        default_schema_name: The schema used when none is configured. ``None``
            means "the database name is the schema" (MySQL); ``""`` means no
            schema is assumed (tables are referenced unqualified).
        row_count_sql: Approximate row-count query taking ``:schema`` and
            ``:table_name`` and returning a ``row_count`` column, or ``None``.
        comment_queries: Table/column comment queries, or ``None`` when the
            engine has no comment catalog.
    """

    name: str
    placeholder_style: PlaceholderStyle
    quote_chars: tuple[str, str]
    like_operator: str
    returning_style: ReturningStyle
    limit_style: LimitStyle
    escape_percent: bool = False
    schema_qualified: bool = True
    default_schema_name: str | None = None
    row_count_sql: str | None = None
    comment_queries: CommentQueries | None = None

    def placeholder(self, index: int) -> str:
        """Positional placeholder for the 1-based ``index``."""
        if self.placeholder_style == "numbered":
            return f"${index}"
        if self.placeholder_style == "format":
            return "%s"
        return "?"

    def quote(self, name: str) -> str:
        """Validate and quote an identifier for this dialect."""
        safe = sanitize_identifier(name)
        open_q, close_q = self.quote_chars
        return f"{open_q}{safe}{close_q}"

    def default_schema(self, database: str, options: Mapping[str, Any] | None = None) -> str:
        """Schema to introspect: ``options["schema"]`` wins, then the dialect default."""
        configured = (options or {}).get("schema")
        if configured:
            return str(configured)
        if self.default_schema_name is not None:
            return self.default_schema_name
        return database

    def order_and_limit(self, order_sql: str, limit: int | None, offset: int = 0) -> str:
        """Compose the ``ORDER BY`` and pagination tail of a SELECT.

        ``limit=None`` means no pagination. SQL Server's ``OFFSET ... FETCH``
        requires an ``ORDER BY``, so a stable dummy ordering is injected when
        the caller did not sort.
        """
        if limit is None:
            return order_sql
        if self.limit_style == "offset_fetch":
            order = order_sql or "ORDER BY (SELECT NULL)"
            return f"{order} OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY"
        tail = f"LIMIT {limit} OFFSET {offset}"
        return f"{order_sql} {tail}" if order_sql else tail

    def sample_select(self, table_sql: str, limit: int) -> str:
        """``SELECT *`` of at most ``limit`` rows from an already-quoted table."""
        if self.limit_style == "offset_fetch":
            return f"SELECT TOP ({limit}) * FROM {table_sql}"
        return f"SELECT * FROM {table_sql} LIMIT {limit}"


# --- PostgreSQL ---------------------------------------------------------------

_PG_ROW_COUNT_SQL = """
                SELECT reltuples::bigint as row_count
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = :table_name AND n.nspname = :schema
                """

_PG_COMMENTS = CommentQueries(
    table="""
SELECT obj_description(c.oid) as comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = :table_name AND n.nspname = :schema
""",
    columns="""
SELECT a.attname as column_name, col_description(a.attrelid, a.attnum) as comment
FROM pg_attribute a
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = :table_name
  AND n.nspname = :schema
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND col_description(a.attrelid, a.attnum) IS NOT NULL
""",
    all_tables="""
SELECT c.relname as table_name, obj_description(c.oid) as comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :schema
  AND c.relkind = 'r'
  AND obj_description(c.oid) IS NOT NULL
""",
    all_columns="""
SELECT c.relname as table_name, a.attname as column_name,
       col_description(a.attrelid, a.attnum) as comment
FROM pg_attribute a
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :schema
  AND c.relkind = 'r'
  AND a.attnum > 0
  AND NOT a.attisdropped
  AND col_description(a.attrelid, a.attnum) IS NOT NULL
""",
    scope_param="schema",
)

POSTGRESQL = Dialect(
    name="postgresql",
    placeholder_style="numbered",
    quote_chars=('"', '"'),
    like_operator="ILIKE",
    returning_style="returning",
    limit_style="limit_offset",
    default_schema_name="public",
    row_count_sql=_PG_ROW_COUNT_SQL,
    comment_queries=_PG_COMMENTS,
)

# --- MySQL / MariaDB ------------------------------------------------------------

_MYSQL_ROW_COUNT_SQL = """
                SELECT TABLE_ROWS as row_count
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table_name
                """

_MYSQL_COMMENTS = CommentQueries(
    table="""
SELECT TABLE_COMMENT as comment
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table_name AND TABLE_COMMENT != ''
""",
    columns="""
SELECT COLUMN_NAME as column_name, COLUMN_COMMENT as comment
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table_name AND COLUMN_COMMENT != ''
""",
    all_tables="""
SELECT TABLE_NAME as table_name, TABLE_COMMENT as comment
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = :database AND TABLE_COMMENT != ''
""",
    all_columns="""
SELECT TABLE_NAME as table_name, COLUMN_NAME as column_name, COLUMN_COMMENT as comment
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = :database AND COLUMN_COMMENT != ''
""",
    scope_param="database",
)

MYSQL = Dialect(
    name="mysql",
    placeholder_style="format",
    quote_chars=("`", "`"),
    like_operator="LIKE",
    returning_style="refetch",
    limit_style="limit_offset",
    escape_percent=True,
    schema_qualified=False,
    default_schema_name=None,
    row_count_sql=_MYSQL_ROW_COUNT_SQL,
    comment_queries=_MYSQL_COMMENTS,
)

# --- Microsoft SQL Server (via ODBC) ----------------------------------------------

_MSSQL_ROW_COUNT_SQL = """
                SELECT SUM(p.rows) AS row_count
                FROM sys.partitions p
                JOIN sys.tables t ON t.object_id = p.object_id
                JOIN sys.schemas s ON s.schema_id = t.schema_id
                WHERE s.name = :schema AND t.name = :table_name AND p.index_id IN (0, 1)
                """

# Comments live in extended properties named MS_Description (class 1 = object
# or column; minor_id 0 = the table itself). `value` is sql_variant, which
# pyodbc cannot read, hence the CAST.
_MSSQL_COMMENTS = CommentQueries(
    table="""
SELECT CAST(ep.value AS nvarchar(max)) AS comment
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = t.object_id AND ep.minor_id = 0
 AND ep.name = 'MS_Description'
WHERE s.name = :schema AND t.name = :table_name
""",
    columns="""
SELECT c.name AS column_name, CAST(ep.value AS nvarchar(max)) AS comment
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id
 AND ep.name = 'MS_Description'
WHERE s.name = :schema AND t.name = :table_name
""",
    all_tables="""
SELECT t.name AS table_name, CAST(ep.value AS nvarchar(max)) AS comment
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = t.object_id AND ep.minor_id = 0
 AND ep.name = 'MS_Description'
WHERE s.name = :schema
""",
    all_columns="""
SELECT t.name AS table_name, c.name AS column_name, CAST(ep.value AS nvarchar(max)) AS comment
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.extended_properties ep
  ON ep.class = 1 AND ep.major_id = c.object_id AND ep.minor_id = c.column_id
 AND ep.name = 'MS_Description'
WHERE s.name = :schema
""",
    scope_param="schema",
)

MSSQL = Dialect(
    name="mssql",
    placeholder_style="qmark",
    quote_chars=("[", "]"),
    like_operator="LIKE",
    returning_style="output",
    limit_style="offset_fetch",
    default_schema_name="dbo",
    row_count_sql=_MSSQL_ROW_COUNT_SQL,
    comment_queries=_MSSQL_COMMENTS,
)

# --- Generic ODBC (best effort: ANSI quoting, no catalog intelligence) -------------

ODBC = Dialect(
    name="odbc",
    placeholder_style="qmark",
    quote_chars=('"', '"'),
    like_operator="LIKE",
    returning_style="refetch",
    limit_style="limit_offset",
    default_schema_name="",
)

DIALECTS: dict[str, Dialect] = {
    "postgresql": POSTGRESQL,
    "postgres": POSTGRESQL,
    "mysql": MYSQL,
    "mariadb": MYSQL,
    "mssql": MSSQL,
    "sqlserver": MSSQL,
    "odbc": ODBC,
}


def get_dialect(dialect: str | Dialect) -> Dialect:
    """Resolve a dialect name (or pass a :class:`Dialect` through).

    Raises:
        ValueError: For an unknown dialect name.
    """
    if isinstance(dialect, Dialect):
        return dialect
    found = DIALECTS.get(dialect.lower())
    if found is None:
        raise ValueError(f"Unsupported SQL dialect: {dialect!r}")
    return found


def dialect_or_generic(dialect: str | Dialect) -> Dialect:
    """Like :func:`get_dialect` but fall back to the generic ODBC profile.

    Used by the metadata readers and the composition root, where an unknown
    engine should degrade to "no catalog intelligence" instead of failing.
    """
    if isinstance(dialect, Dialect):
        return dialect
    found = DIALECTS.get(dialect.lower())
    if found is None:
        logger.warning(f"Unknown SQL dialect {dialect!r}; using the generic ODBC profile")
        return ODBC
    return found
