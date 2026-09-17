"""Database ports: what the application needs from a SQL database."""

from typing import Any, Protocol

from warp.application.config import DatabaseConfig


class SqlReader(Protocol):
    """The narrow read port used by metadata readers and the raw-query use case."""

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a SQL statement (``:name`` placeholders) and return rows as dicts."""
        ...


class DatabaseGateway(Protocol):
    """Full database port: connection lifecycle, introspection and CRUD."""

    name: str

    @property
    def is_connected(self) -> bool:
        """Whether the underlying pool is open."""
        ...

    async def connect(self) -> None:
        """Open the connection pool."""
        ...

    async def disconnect(self) -> None:
        """Close the connection pool."""
        ...

    async def get_tables(self) -> list[str]:
        """List base tables."""
        ...

    async def get_table_schema(self, table: str) -> dict[str, Any]:
        """Describe a table (columns, primary key, foreign keys, indexes)."""
        ...

    async def row_estimates(self, tables: list[str]) -> dict[str, int | None]:
        """Planner row-count statistics per table (never a ``COUNT(*)``).

        Returns ``None`` for a table whose statistics are unknown (never
        analyzed) or that does not exist.
        """
        ...

    async def execute_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a SQL statement (``:name`` placeholders) and return rows as dicts."""
        ...

    async def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        """Insert a record and return it."""
        ...

    async def select(
        self,
        table: str,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        pagination: dict[str, int] | None = None,
        sort: list[tuple[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Select records and the total count."""
        ...

    async def select_by_id(
        self, table: str, id_column: str, id_value: Any, columns: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Select one record by primary key."""
        ...

    async def update(
        self, table: str, id_column: str, id_value: Any, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update one record and return it, or None when it does not exist."""
        ...

    async def delete(self, table: str, id_column: str, id_value: Any) -> bool:
        """Delete one record; True when a row was removed."""
        ...


class DatabaseGatewayFactory(Protocol):
    """Builds a gateway for a configured database (the adapter picks the driver)."""

    def create(self, config: DatabaseConfig | dict[str, Any]) -> DatabaseGateway:
        """Instantiate (but do not connect) a gateway for `config`."""
        ...

    def get_supported_types(self) -> list[str]:
        """Database type names this factory can build."""
        ...
