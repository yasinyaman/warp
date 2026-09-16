"""Sample data reader.

Reads sample rows and column statistics from database tables
using warp's DatabaseAdapter.
"""

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol

from warp.core.logging import get_logger
from warp.database.identifiers import quote_identifier

logger = get_logger(__name__)

# Column-name substrings that indicate likely PII. Sample values for matching
# columns are masked before being sent to an LLM.
DEFAULT_PII_PATTERNS = [
    "email",
    "mail",
    "phone",
    "tel",
    "mobile",
    "ssn",
    "social_security",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credit_card",
    "card_number",
    "cardno",
    "cvv",
    "iban",
    "account_number",
    "tax_id",
    "passport",
    "national_id",
]

_MASK_VALUE = "***"


class DatabaseAdapterProtocol(Protocol):
    """Protocol matching warp's DatabaseAdapter interface."""

    async def execute_query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Run a SQL query and return rows as dicts."""
        ...


@dataclass
class ColumnStats:
    """Statistics for a single column."""

    column_name: str
    distinct_count: int | None = None
    null_count: int | None = None
    sample_values: list[Any] = field(default_factory=list)


@dataclass
class TableSamples:
    """Sample data and stats for a table."""

    table_name: str
    row_count: int | None = None
    column_samples: dict[str, list[Any]] = field(default_factory=dict)
    column_stats: dict[str, ColumnStats] = field(default_factory=dict)


def is_pii_column(name: str, patterns: list[str] | None = None) -> bool:
    """Heuristically decide whether a column name looks like PII."""
    lowered = name.lower()
    return any(p in lowered for p in (patterns or DEFAULT_PII_PATTERNS))


def mask_pii_samples(samples: TableSamples, patterns: list[str] | None = None) -> TableSamples:
    """Return a copy of ``samples`` with PII-looking column values masked.

    Intended for use before sending sample data to an LLM: values of columns
    whose name matches a PII pattern are replaced with ``"***"`` while
    non-PII columns and all column statistics counts are left intact.
    """
    patterns = patterns or DEFAULT_PII_PATTERNS
    masked = copy.deepcopy(samples)

    for col_name, values in masked.column_samples.items():
        if is_pii_column(col_name, patterns):
            masked.column_samples[col_name] = [_MASK_VALUE for _ in values]

    for col_name, stats in masked.column_stats.items():
        if is_pii_column(col_name, patterns) and stats.sample_values:
            stats.sample_values = [_MASK_VALUE for _ in stats.sample_values]

    return masked


class SampleReader:
    """Reads sample data and statistics from database tables."""

    def __init__(
        self,
        adapter: DatabaseAdapterProtocol,
        db_type: str = "postgresql",
        schema: str = "public",
    ):
        """Store the adapter and the dialect/schema to read from."""
        self.adapter = adapter
        self.db_type = db_type.lower()
        self.schema = schema

    def _quote_identifier(self, name: str) -> str:
        """Validate and quote a SQL identifier (dialect-aware, injection-safe).

        Uses the shared strict sanitizer: invalid identifiers raise ValueError
        instead of being silently stripped, so a crafted table/column name can
        never escape the quotes.
        """
        dialect = "mysql" if self.db_type in ("mysql", "mariadb") else "postgresql"
        return quote_identifier(name, dialect)

    def _qualified_table(self, table_name: str) -> str:
        """Get fully qualified table name."""
        if self.db_type == "postgresql":
            return f"{self._quote_identifier(self.schema)}.{self._quote_identifier(table_name)}"
        return self._quote_identifier(table_name)

    async def read_samples(self, table_name: str, limit: int = 5) -> dict[str, list[Any]]:
        """Read sample rows from a table."""
        try:
            qualified = self._qualified_table(table_name)
            query = f"SELECT * FROM {qualified} LIMIT {int(limit)}"
            rows = await self.adapter.execute_query(query)

            if not rows:
                return {}

            result: dict[str, list[Any]] = {}
            for row in rows:
                for col_name, value in row.items():
                    if col_name not in result:
                        result[col_name] = []
                    result[col_name].append(self._serialize_value(value))

            return result

        except Exception as e:
            logger.warning(f"Failed to read sample data for {table_name}: {e}")
            return {}

    async def read_row_count(self, table_name: str) -> int | None:
        """Get approximate row count for a table."""
        try:
            if self.db_type == "postgresql":
                query = """
                SELECT reltuples::bigint as row_count
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = $1 AND n.nspname = $2
                """
                rows = await self.adapter.execute_query(
                    query, {"p1": table_name, "p2": self.schema}
                )
                if rows and rows[0].get("row_count") is not None:
                    count = int(rows[0]["row_count"])
                    return max(count, 0)
            else:
                query = """
                SELECT TABLE_ROWS as row_count
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                """
                rows = await self.adapter.execute_query(
                    query, {"p1": self.schema, "p2": table_name}
                )
                if rows and rows[0].get("row_count") is not None:
                    return int(rows[0]["row_count"])

            return None

        except Exception as e:
            logger.warning(f"Failed to read row count for {table_name}: {e}")
            return None

    async def read_column_stats(
        self, table_name: str, columns: list[str] | None = None
    ) -> dict[str, ColumnStats]:
        """Read basic statistics for columns."""
        result: dict[str, ColumnStats] = {}
        try:
            qualified = self._qualified_table(table_name)
        except ValueError as e:
            logger.warning(f"Failed to read column stats for {table_name}: {e}")
            return result

        if not columns:
            try:
                sample_rows = await self.adapter.execute_query(f"SELECT * FROM {qualified} LIMIT 1")
                if sample_rows:
                    columns = list(sample_rows[0].keys())
                else:
                    return result
            except Exception:
                return result

        for col_name in columns:
            try:
                col_quoted = self._quote_identifier(col_name)
                query = f"""
                SELECT
                    COUNT(DISTINCT {col_quoted}) as distinct_count,
                    SUM(CASE WHEN {col_quoted} IS NULL THEN 1 ELSE 0 END) as null_count
                FROM {qualified}
                """
                rows = await self.adapter.execute_query(query)

                stats = ColumnStats(column_name=col_name)
                if rows:
                    stats.distinct_count = (
                        int(rows[0]["distinct_count"])
                        if rows[0].get("distinct_count") is not None
                        else None
                    )
                    stats.null_count = (
                        int(rows[0]["null_count"])
                        if rows[0].get("null_count") is not None
                        else None
                    )

                result[col_name] = stats

            except Exception as e:
                logger.debug(f"Failed to read column stats for {table_name}.{col_name}: {e}")
                result[col_name] = ColumnStats(column_name=col_name)

        return result

    async def read_table_samples(
        self,
        table_name: str,
        sample_limit: int = 5,
        include_stats: bool = True,
        include_row_count: bool = True,
    ) -> TableSamples:
        """Read comprehensive sample data for a table."""
        samples = TableSamples(table_name=table_name)

        samples.column_samples = await self.read_samples(table_name, limit=sample_limit)

        if include_row_count:
            samples.row_count = await self.read_row_count(table_name)

        if include_stats and samples.column_samples:
            columns = list(samples.column_samples.keys())
            samples.column_stats = await self.read_column_stats(table_name, columns)

            for col_name, stats in samples.column_stats.items():
                if col_name in samples.column_samples:
                    seen = set()
                    unique_samples = []
                    for v in samples.column_samples[col_name]:
                        v_str = str(v)
                        if v_str not in seen and v is not None:
                            seen.add(v_str)
                            unique_samples.append(v)
                    stats.sample_values = unique_samples[:sample_limit]

        logger.info(
            f"Read table samples: {table_name} "
            f"(rows={samples.row_count}, cols={len(samples.column_samples)})"
        )

        return samples

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """Serialize a database value to JSON-compatible format."""
        if value is None:
            return None
        if isinstance(value, str | int | float | bool):
            return value
        return str(value)
