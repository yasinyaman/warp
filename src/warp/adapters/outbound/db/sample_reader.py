"""Sample data reader.

Reads sample rows and column statistics from database tables
using warp's DatabaseAdapter. Dialect differences (row limiting, schema
qualification, the row-count catalog query) come from the :class:`Dialect`.
"""

import logging
from typing import Any

from warp.adapters.outbound.db.dialect import Dialect, dialect_or_generic
from warp.application.ports.database import SqlReader
from warp.domain.samples import ColumnStats, TableSamples

logger = logging.getLogger(__name__)


class SampleReader:
    """Reads sample data and statistics from database tables."""

    def __init__(
        self,
        adapter: SqlReader,
        db_type: str | Dialect = "postgresql",
        schema: str = "public",
    ):
        """Store the adapter and the dialect/schema to read from."""
        self.adapter = adapter
        self.dialect = dialect_or_generic(db_type)
        self.db_type = self.dialect.name
        self.schema = schema

    def _quote_identifier(self, name: str) -> str:
        """Validate and quote a SQL identifier (dialect-aware, injection-safe).

        Uses the shared strict sanitizer: invalid identifiers raise ValueError
        instead of being silently stripped, so a crafted table/column name can
        never escape the quotes.
        """
        return self.dialect.quote(name)

    def _qualified_table(self, table_name: str) -> str:
        """Get fully qualified table name."""
        if self.dialect.schema_qualified and self.schema:
            return f"{self._quote_identifier(self.schema)}.{self._quote_identifier(table_name)}"
        return self._quote_identifier(table_name)

    async def read_samples(self, table_name: str, limit: int = 5) -> dict[str, list[Any]]:
        """Read sample rows from a table."""
        try:
            qualified = self._qualified_table(table_name)
            query = self.dialect.sample_select(qualified, int(limit))
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
        """Get approximate row count for a table (None when unavailable)."""
        query = self.dialect.row_count_sql
        if query is None:
            logger.debug(f"Row counts are not available for dialect {self.db_type}")
            return None
        try:
            rows = await self.adapter.execute_query(
                query, {"schema": self.schema, "table_name": table_name}
            )
            if rows and rows[0].get("row_count") is not None:
                # PostgreSQL reports -1 for tables that were never analyzed.
                return max(int(rows[0]["row_count"]), 0)
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
                sample_rows = await self.adapter.execute_query(
                    self.dialect.sample_select(qualified, 1)
                )
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
                    # Read by position: some drivers upper-case result names
                    # (Oracle returns DISTINCT_COUNT for `as distinct_count`),
                    # and the query selects exactly these two, in this order.
                    values = list(rows[0].values())
                    distinct, nulls = (values + [None, None])[:2]
                    stats.distinct_count = int(distinct) if distinct is not None else None
                    stats.null_count = int(nulls) if nulls is not None else None

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
