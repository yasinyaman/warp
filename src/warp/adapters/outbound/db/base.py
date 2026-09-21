"""Abstract database adapter interface.

All database implementations must inherit from this class.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any


class DatabaseAdapter(ABC):
    """Abstract base class for database adapters.

    This class defines the interface that all database implementations must follow.
    To add support for a new database, create a new class that inherits from this
    and implements all abstract methods.
    """

    def __init__(self, config: dict[str, Any]):
        """Initialize the adapter with configuration.

        Args:
            config: Database configuration dictionary containing:
                   - host, port, database, username, password
                   - options (pool_size, ssl, etc.)
        """
        self.config = config
        self.name = config.get("name", "default")
        self._pool: Any = None

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the database.

        Should create a connection pool for better performance.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Close all connections and cleanup resources."""
        pass

    @abstractmethod
    async def get_tables(self) -> list[str]:
        """Get list of all table names in the database.

        Returns:
            List of table names.
        """
        pass

    @abstractmethod
    async def get_table_schema(self, table: str) -> dict[str, Any]:
        """Get detailed schema information for a specific table.

        Args:
            table: Name of the table to analyze.

        Returns:
            Dictionary containing:
            - columns: List of column definitions
            - primary_key: Primary key column(s)
            - foreign_keys: Foreign key relationships
            - indexes: Index definitions
        """
        pass

    @abstractmethod
    async def row_estimates(self, tables: list[str]) -> dict[str, int | None]:
        """Get the planner's row-count estimate for each table.

        Cheap (catalog statistics only, never ``COUNT(*)``) and therefore
        approximate; ``None`` when the database has no statistics for a table.

        Args:
            tables: Table names to look up.

        Returns:
            Mapping of table name to estimated row count (or None).
        """
        pass

    @abstractmethod
    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Execute a raw SQL query.

        Args:
            query: SQL query string.
            params: Optional parameters for parameterized queries.

        Returns:
            List of dictionaries representing rows.
        """
        pass

    @abstractmethod
    async def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a new record into a table.

        Args:
            table: Table name.
            data: Dictionary of column-value pairs.

        Returns:
            The inserted record with generated values (like ID).
        """
        pass

    @abstractmethod
    async def select(
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        pagination: dict[str, int] | None = None,
        sort: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Select records from a table with filtering, pagination, and sorting.

        Args:
            table: Table name.
            columns: List of columns to select (None = all).
            filters: List of (column, operator, value) tuples.
                    Operators: eq, ne, gt, gte, lt, lte, like, in, is_null
            pagination: Dict with 'limit' and 'offset'.
            sort: List of (column, direction) tuples. Direction: 'asc' or 'desc'.

        Returns:
            Tuple of (list of records, total count without pagination).
        """
        pass

    @abstractmethod
    def stream_select(  # noqa: PLR0913
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        sort: list[tuple[str, str]] | None = None,
        batch_size: int = 5000,
        limit: int | None = None,
        statement_timeout_ms: int = 0,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Stream matching rows in batches through a server-side cursor.

        Unlike ``select`` there is no ``COUNT(*)`` and no ``OFFSET``: the
        statement runs once and rows are pulled ``batch_size`` at a time, so
        memory stays flat however large the result is.

        Args:
            table: Table name.
            columns: Columns to select (None = all).
            filters: ``(column, operator, value)`` triples (see ``select``).
            sort: ``(column, direction)`` pairs.
            batch_size: Rows per yielded batch.
            limit: Optional hard cap on the number of rows.
            statement_timeout_ms: Per-statement timeout (0 = driver default).

        Yields:
            Lists of row dicts, in order.
        """
        pass

    @abstractmethod
    async def select_by_id(
        self, table: str, key: Mapping[str, Any], columns: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Select a single record by its ID.

        Args:
            table: Table name.
            key: Column-to-value map for the whole primary key.
            columns: List of columns to select (None = all).

        Returns:
            Record dictionary or None if not found.
        """
        pass

    @abstractmethod
    async def update(
        self, table: str, key: Mapping[str, Any], data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an existing record.

        Args:
            table: Table name.
            key: Column-to-value map for the whole primary key.
            data: Dictionary of column-value pairs to update.

        Returns:
            The updated record or None if not found.
        """
        pass

    @abstractmethod
    async def delete(self, table: str, key: Mapping[str, Any]) -> bool:
        """Delete a record by its ID.

        Args:
            table: Table name.
            key: Column-to-value map for the whole primary key.

        Returns:
            True if deleted, False if not found.
        """
        pass

    @property
    def is_connected(self) -> bool:
        """Check if database is connected."""
        return self._pool is not None
