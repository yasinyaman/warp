"""
Database factory for creating database adapters.
"""
from typing import Any

from .base import DatabaseAdapter
from .mysql import MySQLAdapter
from .postgres import PostgreSQLAdapter

# Registry of available database adapters
ADAPTERS: dict[str, type[DatabaseAdapter]] = {
    "postgresql": PostgreSQLAdapter,
    "postgres": PostgreSQLAdapter,
    "mysql": MySQLAdapter,
    "mariadb": MySQLAdapter,
}


class DatabaseFactory:
    """
    Factory class for creating database adapters.

    Usage:
        adapter = DatabaseFactory.create(config)
        await adapter.connect()

    To add a new database type:
        1. Create a new adapter class inheriting from DatabaseAdapter
        2. Register it in the ADAPTERS dictionary
    """

    @staticmethod
    def create(config: dict[str, Any]) -> DatabaseAdapter:
        """
        Create a database adapter based on configuration.

        Args:
            config: Database configuration dictionary containing:
                   - type: Database type (postgresql, mysql, etc.)
                   - host, port, database, username, password
                   - options: Additional options

        Returns:
            DatabaseAdapter instance.

        Raises:
            ValueError: If database type is not supported.
        """
        db_type = config.get("type", "").lower()

        if db_type not in ADAPTERS:
            supported = ", ".join(ADAPTERS.keys())
            raise ValueError(
                f"Unsupported database type: {db_type}. "
                f"Supported types: {supported}"
            )

        adapter_class = ADAPTERS[db_type]
        return adapter_class(config)

    @staticmethod
    def register(db_type: str, adapter_class: type[DatabaseAdapter]) -> None:
        """
        Register a new database adapter type.

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
        """
        Get list of supported database types.

        Returns:
            List of supported database type names.
        """
        return list(ADAPTERS.keys())
