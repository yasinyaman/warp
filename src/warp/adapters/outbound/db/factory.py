"""Database factory for creating database adapters."""

from typing import Any

from warp.adapters.outbound.db.base import DatabaseAdapter
from warp.adapters.outbound.db.mysql import MySQLAdapter
from warp.adapters.outbound.db.odbc import ODBCAdapter
from warp.adapters.outbound.db.postgres import PostgreSQLAdapter
from warp.application.config import DatabaseConfig

# Registry of available database adapters
ADAPTERS: dict[str, type[DatabaseAdapter]] = {
    "postgresql": PostgreSQLAdapter,
    "postgres": PostgreSQLAdapter,
    "mysql": MySQLAdapter,
    "mariadb": MySQLAdapter,
    # ODBC (aioodbc/pyodbc): SQL Server profile and a generic best-effort profile.
    "mssql": ODBCAdapter,
    "sqlserver": ODBCAdapter,
    "odbc": ODBCAdapter,
}


class DatabaseFactory:
    """Factory class for creating database adapters.

    Usage:
        adapter = DatabaseFactory.create(config)
        await adapter.connect()

    To add a new database type:
        1. Create a new adapter class inheriting from DatabaseAdapter
        2. Register it in the ADAPTERS dictionary
    """

    @staticmethod
    def create(config: DatabaseConfig | dict[str, Any]) -> DatabaseAdapter:
        """Create a database adapter based on configuration.

        Args:
            config: A `DatabaseConfig` (preferred; `options` stay nested so pool
                   sizing/SSL reach the adapter) or the equivalent dictionary with
                   `type`, `host`, `port`, `database`, `username`, `password`
                   and an optional nested `options` mapping.

        Returns:
            DatabaseAdapter instance.

        Raises:
            ValueError: If database type is not supported.
        """
        if isinstance(config, DatabaseConfig):
            config = config.model_dump()
        db_type = config.get("type", "").lower()

        if db_type not in ADAPTERS:
            supported = ", ".join(ADAPTERS.keys())
            raise ValueError(f"Unsupported database type: {db_type}. Supported types: {supported}")

        adapter_class = ADAPTERS[db_type]
        return adapter_class(config)

    @staticmethod
    def register(db_type: str, adapter_class: type[DatabaseAdapter]) -> None:
        """Register a new database adapter type.

        Args:
            db_type: Database type name (e.g., 'sqlite', 'oracle').
            adapter_class: Adapter class inheriting from DatabaseAdapter.

        Example:
            from my_adapters import SQLiteAdapter
            DatabaseFactory.register('sqlite', SQLiteAdapter)
        """
        ADAPTERS[db_type.lower()] = adapter_class

    @staticmethod
    def get_supported_types() -> list[str]:
        """Get list of supported database types.

        Returns:
            List of supported database type names.
        """
        return list(ADAPTERS.keys())
