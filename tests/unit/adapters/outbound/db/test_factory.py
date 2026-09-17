"""Tests for DatabaseFactory."""

from typing import Any

import pytest

from warp.adapters.outbound.db.base import DatabaseAdapter
from warp.adapters.outbound.db.factory import DatabaseFactory
from warp.adapters.outbound.db.mysql import MySQLAdapter
from warp.adapters.outbound.db.odbc import ODBCAdapter
from warp.adapters.outbound.db.postgres import PostgreSQLAdapter


def test_create_postgresql() -> None:
    adapter = DatabaseFactory.create({"type": "postgresql", "database": "d"})
    assert isinstance(adapter, PostgreSQLAdapter)


def test_create_postgres_alias() -> None:
    assert isinstance(
        DatabaseFactory.create({"type": "postgres", "database": "d"}),
        PostgreSQLAdapter,
    )


def test_create_mysql_and_mariadb() -> None:
    assert isinstance(DatabaseFactory.create({"type": "mysql", "database": "d"}), MySQLAdapter)
    assert isinstance(DatabaseFactory.create({"type": "mariadb", "database": "d"}), MySQLAdapter)


def test_create_case_insensitive() -> None:
    assert isinstance(
        DatabaseFactory.create({"type": "PostgreSQL", "database": "d"}),
        PostgreSQLAdapter,
    )


def test_create_unsupported_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported database type"):
        DatabaseFactory.create({"type": "sqlite", "database": "d"})


def test_create_missing_type_raises() -> None:
    with pytest.raises(ValueError):
        DatabaseFactory.create({"database": "d"})


def test_get_supported_types() -> None:
    types = DatabaseFactory.get_supported_types()
    assert "postgresql" in types
    assert "mysql" in types


def test_register_new_adapter() -> None:
    class DummyAdapter(DatabaseAdapter):
        async def connect(self) -> None: ...
        async def disconnect(self) -> None: ...
        async def get_tables(self) -> list[str]:
            return []

        async def get_table_schema(self, table: str) -> dict[str, Any]:
            return {}

        async def execute_query(
            self, query: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            return []

        async def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def select(self, *a: Any, **k: Any) -> tuple[list[dict[str, Any]], int]:
            return [], 0

        async def select_by_id(self, *a: Any, **k: Any) -> dict[str, Any] | None:
            return None

        async def update(self, *a: Any, **k: Any) -> dict[str, Any] | None:
            return None

        async def delete(self, *a: Any, **k: Any) -> bool:
            return False

    DatabaseFactory.register("dummy", DummyAdapter)
    try:
        assert isinstance(DatabaseFactory.create({"type": "dummy", "database": "d"}), DummyAdapter)
    finally:
        from warp.adapters.outbound.db.factory import ADAPTERS

        ADAPTERS.pop("dummy", None)


def test_database_config_keeps_options_nested() -> None:
    from warp.application.config import DatabaseConfig

    cfg = DatabaseConfig(
        name="main",
        type="postgresql",
        database="d",
        username="u",
        options={"pool_size": 3, "ssl": True},
    )
    adapter = DatabaseFactory.create(cfg)
    assert isinstance(adapter, PostgreSQLAdapter)
    assert adapter.name == "main"
    assert adapter.config["options"] == {"pool_size": 3, "ssl": True}


@pytest.mark.parametrize("db_type", ["mssql", "sqlserver", "odbc", "MSSQL"])
def test_create_odbc_variants(db_type: str) -> None:
    adapter = DatabaseFactory.create({"type": db_type, "database": "d"})
    assert isinstance(adapter, ODBCAdapter)


def test_odbc_types_are_advertised() -> None:
    assert {"mssql", "sqlserver", "odbc"} <= set(DatabaseFactory.get_supported_types())
