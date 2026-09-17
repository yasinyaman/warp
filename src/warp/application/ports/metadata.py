"""Metadata ports: comments and sample data read from a live database."""

from typing import Protocol

from warp.domain.comments import TableComments
from warp.domain.samples import TableSamples


class CommentSource(Protocol):
    """Reads table/column comments stored in the database."""

    async def read_all_comments(self, table_names: list[str]) -> dict[str, TableComments]:
        """Comments for each of `table_names` (missing tables are absent)."""
        ...


class SampleSource(Protocol):
    """Reads sample rows and basic statistics."""

    async def read_table_samples(
        self,
        table_name: str,
        sample_limit: int = 5,
        include_stats: bool = True,
        include_row_count: bool = True,
    ) -> TableSamples:
        """Sample values per column plus optional row count and stats."""
        ...
