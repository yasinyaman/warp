"""Unit tests for ODBCAdapter over a fake aioodbc pool (no ODBC driver needed).

The adapter uses `async with pool.acquire() as conn, conn.cursor() as cur`;
the fakes provide both layers. A FakeCursor consumes one queued ResultSet per
execute()/catalog call, exposing `description`/`fetchall()`/`rowcount` the way
pyodbc does, so the adapter's row/dict conversion and result handling are
exercised for real.
"""

from __future__ import annotations

import logging
import struct
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from warp.adapters.outbound.db import odbc as odbc_module
from warp.adapters.outbound.db.dialect import MSSQL, ODBC
from warp.adapters.outbound.db.odbc import (
    SQL_SS_TIMESTAMPOFFSET,
    ODBCAdapter,
    build_connection_string,
    configure_mssql_connection,
    decode_datetimeoffset,
)

MSSQL_CONFIG: dict[str, Any] = {
    "name": "ms",
    "type": "mssql",
    "host": "db.example",
    "port": 1433,
    "database": "shop",
    "username": "sa",
    "password": "s3cret",
}

GENERIC_CONFIG: dict[str, Any] = {
    "name": "gen",
    "type": "odbc",
    "host": "h",
    "port": 1521,
    "database": "orcl",
    "username": "u",
    "password": "p",
    "options": {"driver": "Oracle 21 ODBC driver", "schema": "APP"},
}


class ResultSet:
    """What one statement produces: columns + rows, or nothing (DDL/DML)."""

    def __init__(
        self,
        columns: list[str] | None = None,
        rows: list[tuple[Any, ...]] | None = None,
        rowcount: int | None = None,
    ) -> None:
        self.columns = columns
        self.rows = rows or []
        self.rowcount = rowcount


NO_RESULT = ResultSet()


class FakeCursor:
    def __init__(self, results: list[ResultSet | Exception] | None = None) -> None:
        self.results = list(results or [])
        self.executed: list[tuple[str, Any]] = []
        self.catalog_calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.description: list[tuple[Any, ...]] | None = None
        self.rowcount = -1
        self._rows: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def _consume(self) -> None:
        current = self.results.pop(0) if self.results else ResultSet()
        if isinstance(current, Exception):
            raise current
        if current.columns is None:
            self.description = None
        else:
            self.description = [
                (name, str, None, None, None, None, True) for name in current.columns
            ]
        self._rows = list(current.rows)
        if current.rowcount is not None:
            self.rowcount = current.rowcount
        else:
            self.rowcount = len(self._rows) if current.columns is not None else -1

    async def execute(self, sql: str, params: Any = None) -> FakeCursor:
        self.executed.append((sql, params))
        self._consume()
        return self

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows.pop(0) if self._rows else None

    async def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
        batch, self._rows = self._rows[:size], self._rows[size:]
        return batch

    # ODBC catalog functions (generic profile)
    async def tables(self, *args: Any, **kwargs: Any) -> None:
        self.catalog_calls.append(("tables", args, kwargs))
        self._consume()

    async def columns(self, *args: Any, **kwargs: Any) -> None:
        self.catalog_calls.append(("columns", args, kwargs))
        self._consume()

    async def primaryKeys(self, *args: Any, **kwargs: Any) -> None:  # noqa: N802
        self.catalog_calls.append(("primaryKeys", args, kwargs))
        self._consume()

    async def foreignKeys(self, *args: Any, **kwargs: Any) -> None:  # noqa: N802
        self.catalog_calls.append(("foreignKeys", args, kwargs))
        self._consume()

    async def statistics(self, *args: Any, **kwargs: Any) -> None:
        self.catalog_calls.append(("statistics", args, kwargs))
        self._consume()


class QuirkyStatisticsCursor(FakeCursor):
    """aioodbc 0.5's `statistics()` wrapper cannot pass the table name."""

    def __init__(self, results: list[ResultSet | Exception] | None = None) -> None:
        super().__init__(results)
        self._impl = SimpleNamespace(statistics=object())

    async def statistics(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("statistics() missing required argument 'table'")

    async def _run_operation(self, func: Any, *args: Any, **kwargs: Any) -> None:
        assert func is self._impl.statistics
        self.catalog_calls.append(("statistics_impl", args, kwargs))
        self._consume()


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> FakeConn:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)
        self.closed = False
        self.waited = False

    def acquire(self) -> FakeConn:
        return self._conn

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


def make_adapter(config: dict[str, Any], cursor: FakeCursor) -> ODBCAdapter:
    adapter = ODBCAdapter(config)
    adapter._pool = FakePool(cursor)
    return adapter


# --- connection string --------------------------------------------------------------


class TestConnectionString:
    def test_mssql_defaults(self):
        assert build_connection_string(MSSQL_CONFIG) == (
            "DRIVER={ODBC Driver 18 for SQL Server};SERVER=db.example,1433;DATABASE=shop;"
            "UID=sa;PWD=s3cret;Encrypt=yes;TrustServerCertificate=no;"
        )

    def test_mssql_options(self):
        config = {
            **MSSQL_CONFIG,
            "options": {
                "driver": "{ODBC Driver 17 for SQL Server}",
                "encrypt": False,
                "trust_server_certificate": True,
                "extra": "ApplicationIntent=ReadOnly;",
            },
        }
        assert build_connection_string(config) == (
            "DRIVER={ODBC Driver 17 for SQL Server};SERVER=db.example,1433;DATABASE=shop;"
            "UID=sa;PWD=s3cret;Encrypt=no;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;"
        )

    def test_encrypt_keywords_pass_through(self):
        dsn = build_connection_string({**MSSQL_CONFIG, "options": {"encrypt": "strict"}})
        assert "Encrypt=strict;" in dsn

    def test_special_characters_are_brace_quoted(self):
        dsn = build_connection_string({**MSSQL_CONFIG, "password": "p;w}d "})
        assert "PWD={p;w}}d };" in dsn

    def test_no_port_and_named_instance(self):
        dsn = build_connection_string({**MSSQL_CONFIG, "host": r"db\SQLEXPRESS", "port": None})
        assert r"SERVER=db\SQLEXPRESS;" in dsn

    def test_verbatim_connection_string_gets_credentials(self):
        config = {**MSSQL_CONFIG, "options": {"connection_string": "DSN=warp;Encrypt=no"}}
        assert build_connection_string(config) == "DSN=warp;Encrypt=no;UID=sa;PWD=s3cret;"

    def test_verbatim_connection_string_with_uid_is_untouched(self):
        config = {**MSSQL_CONFIG, "options": {"connection_string": "DSN=warp;UID=app;PWD=x;"}}
        assert build_connection_string(config) == "DSN=warp;UID=app;PWD=x;"

    def test_generic_needs_a_driver(self):
        with pytest.raises(ValueError, match="options.driver"):
            build_connection_string({**GENERIC_CONFIG, "options": {}})

    def test_generic_has_no_sqlserver_keywords(self):
        dsn = build_connection_string(GENERIC_CONFIG)
        assert dsn == "DRIVER={Oracle 21 ODBC driver};SERVER=h,1521;DATABASE=orcl;UID=u;PWD=p;"
        assert "Encrypt" not in dsn

    def test_readonly_config_keeps_working(self):
        from warp.application.config import DatabaseConfig

        cfg = DatabaseConfig(
            name="ms",
            type="mssql",
            database="shop",
            username="sa",
            password="x",
            readonly_username="reader",
            readonly_password="r",
            options={"connection_string": "DSN=warp"},
        )
        ro = cfg.readonly_config()
        assert ro is not None
        assert build_connection_string(ro) == "DSN=warp;UID=reader;PWD=r;"


# --- driver quirks ------------------------------------------------------------------


class TestDatetimeOffset:
    def test_decode(self):
        raw = struct.pack("<6hI2h", 2024, 3, 9, 14, 30, 15, 123_456_000, 3, 0)
        assert decode_datetimeoffset(raw) == datetime(
            2024, 3, 9, 14, 30, 15, 123_456, tzinfo=timezone(timedelta(hours=3))
        )

    def test_negative_offset(self):
        raw = struct.pack("<6hI2h", 2024, 1, 1, 0, 0, 0, 0, -5, -30)
        assert decode_datetimeoffset(raw).utcoffset() == timedelta(hours=-5, minutes=-30)

    @pytest.mark.asyncio
    async def test_hook_registers_converter(self):
        registered: dict[int, Any] = {}
        conn = SimpleNamespace(
            add_output_converter=lambda code, func: registered.__setitem__(code, func)
        )
        await configure_mssql_connection(conn)
        assert registered == {SQL_SS_TIMESTAMPOFFSET: decode_datetimeoffset}
        assert SQL_SS_TIMESTAMPOFFSET == -155


# --- lifecycle -----------------------------------------------------------------------


class TestConnect:
    @pytest.fixture
    def fake_aioodbc(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        async def create_pool(**kwargs: Any) -> FakePool:
            captured.update(kwargs)
            return FakePool(FakeCursor())

        module = types.ModuleType("aioodbc")
        module.create_pool = create_pool  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "aioodbc", module)
        return captured

    @pytest.mark.asyncio
    async def test_mssql_pool(self, fake_aioodbc: dict[str, Any]):
        adapter = ODBCAdapter(
            {**MSSQL_CONFIG, "options": {"pool_size": 4, "pool_min_size": 1, "pool_recycle": 300}}
        )
        assert adapter.is_connected is False
        await adapter.connect()
        assert adapter.is_connected is True
        assert fake_aioodbc["dsn"].startswith("DRIVER={ODBC Driver 18 for SQL Server};")
        assert (fake_aioodbc["minsize"], fake_aioodbc["maxsize"]) == (1, 4)
        assert fake_aioodbc["pool_recycle"] == 300
        assert fake_aioodbc["autocommit"] is True
        assert fake_aioodbc["after_created"] is configure_mssql_connection

    @pytest.mark.asyncio
    async def test_generic_pool_has_no_mssql_hook(self, fake_aioodbc: dict[str, Any]):
        await ODBCAdapter(GENERIC_CONFIG).connect()
        assert fake_aioodbc["after_created"] is None
        assert (fake_aioodbc["minsize"], fake_aioodbc["maxsize"]) == (2, 10)

    @pytest.mark.asyncio
    async def test_missing_aioodbc_is_explained(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setitem(sys.modules, "aioodbc", None)
        with pytest.raises(RuntimeError, match=r"warp-engine\[odbc\]"):
            await ODBCAdapter(MSSQL_CONFIG).connect()

    def test_module_does_not_import_the_driver_eagerly(self):
        assert "aioodbc" not in vars(odbc_module)
        assert "pyodbc" not in vars(odbc_module)

    @pytest.mark.asyncio
    async def test_disconnect(self):
        adapter = make_adapter(MSSQL_CONFIG, FakeCursor())
        pool = adapter._pool
        await adapter.disconnect()
        assert pool.closed and pool.waited
        assert adapter._pool is None
        await adapter.disconnect()  # idempotent

    def test_profiles(self):
        assert ODBCAdapter(MSSQL_CONFIG).dialect is MSSQL
        assert ODBCAdapter({**MSSQL_CONFIG, "type": "SQLServer"}).dialect is MSSQL
        assert ODBCAdapter(GENERIC_CONFIG).dialect is ODBC
        assert ODBCAdapter(MSSQL_CONFIG)._schema == "dbo"
        assert ODBCAdapter({**MSSQL_CONFIG, "options": {"schema": "sales"}})._schema == "sales"
        assert ODBCAdapter(GENERIC_CONFIG)._schema == "APP"


# --- raw SQL --------------------------------------------------------------------------


class TestExecuteQuery:
    @pytest.mark.asyncio
    async def test_binds_named_params_as_qmark(self):
        cur = FakeCursor([ResultSet(["id", "name"], [(1, "a"), (2, "b")])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        rows = await adapter.execute_query(
            "SELECT id, name FROM users WHERE id > :min AND name LIKE :p", {"min": 0, "p": "%a%"}
        )
        assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        assert cur.executed == [
            ("SELECT id, name FROM users WHERE id > ? AND name LIKE ?", [0, "%a%"])
        ]

    @pytest.mark.asyncio
    async def test_no_params_runs_the_statement_as_is(self):
        cur = FakeCursor([NO_RESULT])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.execute_query("CREATE TABLE t (id int)") == []
        assert cur.executed == [("CREATE TABLE t (id int)", None)]


# --- introspection ------------------------------------------------------------------


class TestSQLServerIntrospection:
    @pytest.mark.asyncio
    async def test_get_tables(self):
        cur = FakeCursor([ResultSet(["table_name"], [("orders",), ("users",)])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.get_tables() == ["orders", "users"]
        sql, params = cur.executed[0]
        assert "INFORMATION_SCHEMA.TABLES" in sql and params == ["dbo"]

    @pytest.mark.asyncio
    async def test_get_table_schema(self):
        column_names = [
            "column_name",
            "data_type",
            "is_nullable",
            "column_default",
            "max_length",
            "numeric_precision",
            "numeric_scale",
            "is_identity",
            "is_computed",
        ]
        columns = ResultSet(
            column_names,
            [
                ("id", "int", "NO", None, None, 10, 0, 1, 0),
                ("name", "nvarchar", "NO", None, 255, None, None, 0, 0),
                ("bio", "nvarchar", "YES", None, -1, None, None, 0, 0),
                ("price", "decimal", "NO", "((0.00))", None, 10, 2, 0, 0),
                ("active", "bit", "NO", "((1))", None, None, None, 0, 0),
                ("total", "money", "YES", None, None, 19, 4, 0, 1),
                ("rv", "timestamp", "NO", None, 8, None, None, 0, 0),
                ("created", "datetime2", "NO", "(sysutcdatetime())", None, None, None, 0, 0),
            ],
        )
        pk = ResultSet(["column_name"], [("id",)])
        fks = ResultSet(
            ["column_name", "foreign_table", "foreign_column", "constraint_name"],
            [("customer_id", "customers", "id", "FK_orders_customers")],
        )
        idx = ResultSet(
            ["index_name", "column_name", "is_unique"],
            [
                ("IX_name_active", "name", True),
                ("IX_name_active", "active", True),
                ("IX_created", "created", False),
            ],
        )
        cur = FakeCursor([columns, pk, fks, idx])
        adapter = make_adapter(MSSQL_CONFIG, cur)

        schema = await adapter.get_table_schema("orders")

        assert schema["table_name"] == "orders"
        assert schema["primary_key"] == "id"
        by_name = {c["name"]: c for c in schema["columns"]}
        assert by_name["id"] == {
            "name": "id",
            "type": "int",
            "full_type": "int",
            "nullable": False,
            "default": None,
            "max_length": None,
            "precision": 10,
            "scale": 0,
            "key": "PRI",
            "extra": "identity",
        }
        assert by_name["name"]["full_type"] == "nvarchar(255)"
        assert by_name["bio"]["full_type"] == "nvarchar(max)" and by_name["bio"]["nullable"]
        assert by_name["price"]["full_type"] == "decimal(10,2)"
        assert by_name["price"]["default"] == "0.00"
        assert by_name["active"]["default"] == "1"
        assert by_name["total"]["extra"] == "computed"
        assert by_name["rv"]["type"] == "rowversion"
        assert by_name["created"]["default"] == "sysutcdatetime()"
        assert schema["foreign_keys"] == [
            {
                "column": "customer_id",
                "references_table": "customers",
                "references_column": "id",
                "constraint_name": "FK_orders_customers",
            }
        ]
        assert schema["indexes"] == [
            {"name": "IX_name_active", "columns": ["name", "active"], "unique": True},
            {"name": "IX_created", "columns": ["created"], "unique": False},
        ]
        # Every catalog statement is scoped to the configured schema + table.
        assert all(params == ["dbo", "orders"] for _, params in cur.executed)
        assert "sys.foreign_keys" in cur.executed[2][0] and "sys.indexes" in cur.executed[3][0]

    @pytest.mark.asyncio
    async def test_composite_primary_key(self):
        cur = FakeCursor(
            [
                ResultSet(["column_name", "data_type", "is_nullable"], []),
                ResultSet(["column_name"], [("order_id",), ("line",)]),
                ResultSet(["column_name"], []),
                ResultSet(["index_name"], []),
            ]
        )
        schema = await make_adapter(MSSQL_CONFIG, cur).get_table_schema("lines")
        assert schema["primary_key"] == ["order_id", "line"]
        assert schema["columns"] == [] and schema["indexes"] == []

    def test_column_only_uses_keys_column_schema_accepts(self):
        from warp.domain.schema import ColumnSchema

        col = ODBCAdapter._mssql_column(
            {
                "column_name": "x",
                "data_type": "varchar",
                "is_nullable": "YES",
                "column_default": "('(a)')",
                "max_length": 10,
                "numeric_precision": None,
                "numeric_scale": None,
                "is_identity": 0,
                "is_computed": 0,
            },
            False,
        )
        assert ColumnSchema(**col).full_type == "varchar(10)"
        assert col["default"] == "'(a)'"


class TestGenericIntrospection:
    # ODBC SQLColumns row layout (18 columns); only the ones the adapter reads matter.
    @staticmethod
    def _column_row(
        name: str,
        type_name: str,
        size: int | None,
        digits: int | None,
        nullable: int,
        default: Any = None,
        is_nullable: str | None = None,
    ) -> tuple[Any, ...]:
        return (
            None,  # table_cat
            "APP",  # table_schem
            "ITEMS",  # table_name
            name,  # column_name
            0,  # data_type
            type_name,  # type_name
            size,  # column_size
            None,  # buffer_length
            digits,  # decimal_digits
            None,  # num_prec_radix
            nullable,  # nullable
            None,  # remarks
            default,  # column_def
            None,  # sql_data_type
            None,  # sql_datetime_sub
            None,  # char_octet_length
            1,  # ordinal_position
            is_nullable if is_nullable is not None else ("YES" if nullable else "NO"),
        )

    @pytest.mark.asyncio
    async def test_get_tables_uses_sqltables(self):
        cur = FakeCursor(
            [
                ResultSet(
                    ["c"],
                    [(None, "APP", "USERS", "TABLE", None), (None, "APP", "ITEMS", "TABLE", None)],
                )
            ]
        )
        adapter = make_adapter(GENERIC_CONFIG, cur)
        assert await adapter.get_tables() == ["ITEMS", "USERS"]
        assert cur.catalog_calls == [("tables", (), {"schema": "APP", "tableType": "TABLE"})]

    @pytest.mark.asyncio
    async def test_get_tables_without_schema(self):
        cur = FakeCursor([ResultSet(["c"], [])])
        adapter = make_adapter({**GENERIC_CONFIG, "options": {"driver": "x"}}, cur)
        assert await adapter.get_tables() == []
        assert cur.catalog_calls[0][2] == {"schema": None, "tableType": "TABLE"}

    @pytest.mark.asyncio
    async def test_get_table_schema(self):
        columns = ResultSet(
            ["c"],
            [
                self._column_row("ID", "int identity", 10, 0, 0),
                self._column_row("NAME", "varchar", 50, None, 1),
                self._column_row("PRICE", "decimal", 10, 2, 0, default="0"),
                self._column_row("NOTE", "text", 100, None, 1, is_nullable=""),
            ],
        )
        pks = ResultSet(["c"], [(None, "APP", "ITEMS", "ID", 1, "PK_ITEMS")])
        fks = ResultSet(
            ["c"],
            [
                (
                    None,
                    "APP",
                    "CATEGORIES",
                    "ID",
                    None,
                    "APP",
                    "ITEMS",
                    "CATEGORY_ID",
                    1,
                    None,
                    None,
                    "FK_ITEMS_CATEGORY",
                    "PK_CATEGORIES",
                    None,
                )
            ],
        )
        stats = ResultSet(
            ["c"],
            [
                (
                    None,
                    "APP",
                    "ITEMS",
                    None,
                    None,
                    None,
                    0,
                    None,
                    None,
                    None,
                    4,
                    1,
                    None,
                ),  # table stat
                (None, "APP", "ITEMS", 0, None, "UX_NAME", 3, 1, "NAME", "A", None, None, None),
                (
                    None,
                    "APP",
                    "ITEMS",
                    1,
                    None,
                    "IX_PRICE_NAME",
                    3,
                    2,
                    "NAME",
                    "A",
                    None,
                    None,
                    None,
                ),
                (
                    None,
                    "APP",
                    "ITEMS",
                    1,
                    None,
                    "IX_PRICE_NAME",
                    3,
                    1,
                    "PRICE",
                    "A",
                    None,
                    None,
                    None,
                ),
            ],
        )
        cur = FakeCursor([columns, pks, fks, stats])
        adapter = make_adapter(GENERIC_CONFIG, cur)

        schema = await adapter.get_table_schema("ITEMS")

        assert schema["primary_key"] == "ID"
        by_name = {c["name"]: c for c in schema["columns"]}
        assert by_name["ID"]["type"] == "int" and by_name["ID"]["extra"] == "identity"
        assert by_name["ID"]["key"] == "PRI" and by_name["ID"]["nullable"] is False
        assert by_name["NAME"] == {
            "name": "NAME",
            "type": "varchar",
            "full_type": "varchar",
            "nullable": True,
            "default": None,
            "max_length": 50,
            "precision": None,
            "scale": None,
            "key": None,
            "extra": None,
        }
        assert by_name["PRICE"]["precision"] == 10 and by_name["PRICE"]["scale"] == 2
        assert by_name["PRICE"]["default"] == "0" and by_name["PRICE"]["max_length"] is None
        assert by_name["NOTE"]["nullable"] is True  # falls back to the NULLABLE flag
        assert schema["foreign_keys"] == [
            {
                "column": "CATEGORY_ID",
                "references_table": "CATEGORIES",
                "references_column": "ID",
                "constraint_name": "FK_ITEMS_CATEGORY",
            }
        ]
        assert schema["indexes"] == [
            {"name": "IX_PRICE_NAME", "columns": ["PRICE", "NAME"], "unique": False},
            {"name": "UX_NAME", "columns": ["NAME"], "unique": True},
        ]
        assert [call[0] for call in cur.catalog_calls] == [
            "columns",
            "primaryKeys",
            "foreignKeys",
            "statistics",
        ]
        assert cur.catalog_calls[0][2] == {"table": "ITEMS", "schema": "APP"}
        assert cur.catalog_calls[1] == ("primaryKeys", ("ITEMS",), {"schema": "APP"})
        assert cur.catalog_calls[2][2] == {"foreignTable": "ITEMS", "foreignSchema": "APP"}

    @pytest.mark.asyncio
    async def test_statistics_falls_back_to_pyodbc_when_wrapper_is_broken(self):
        cur = QuirkyStatisticsCursor(
            [
                ResultSet(["c"], []),
                ResultSet(["c"], []),
                ResultSet(["c"], []),
                ResultSet(
                    ["c"], [(None, "APP", "T", 1, None, "IX", 3, 1, "A", "A", None, None, None)]
                ),
            ]
        )
        schema = await make_adapter(GENERIC_CONFIG, cur).get_table_schema("T")
        assert schema["indexes"] == [{"name": "IX", "columns": ["A"], "unique": False}]
        assert cur.catalog_calls[-1] == ("statistics_impl", ("T",), {"schema": "APP"})

    @pytest.mark.asyncio
    async def test_statistics_failure_degrades_to_no_indexes(self):
        cur = FakeCursor(
            [
                ResultSet(["c"], []),
                ResultSet(["c"], []),
                ResultSet(["c"], []),
                RuntimeError("no stats"),
            ]
        )
        schema = await make_adapter(GENERIC_CONFIG, cur).get_table_schema("T")
        assert schema["indexes"] == [] and schema["primary_key"] is None


# --- CRUD -----------------------------------------------------------------------------


def _schema_results(pk: str | None = "id") -> list[ResultSet]:
    """The four result sets `_mssql_table_schema` consumes (used by `_primary_key`)."""
    return [
        ResultSet(["column_name", "data_type", "is_nullable"], []),
        ResultSet(["column_name"], [(pk,)] if pk else []),
        ResultSet(["column_name"], []),
        ResultSet(["index_name"], []),
    ]


TRIGGER_ERROR = RuntimeError(
    "('42000', \"[42000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]The target "
    "table 'users' of the DML statement cannot have any enabled triggers if the statement "
    'contains an OUTPUT clause without INTO clause. (334) (SQLExecDirectW)")'
)


class TestInsert:
    @pytest.mark.asyncio
    async def test_mssql_output_returns_the_row(self):
        cur = FakeCursor([ResultSet(["id", "name"], [(7, "a")])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.insert("users", {"name": "a"}) == {"id": 7, "name": "a"}
        assert cur.executed == [
            ("INSERT INTO [users] ([name]) OUTPUT INSERTED.* VALUES (?)", ["a"])
        ]

    @pytest.mark.asyncio
    async def test_mssql_trigger_fallback_reselects_by_scope_identity(self):
        cur = FakeCursor(
            [
                TRIGGER_ERROR,
                ResultSet(["new_id"], [(42,)]),
                *_schema_results(),
                ResultSet(["id", "name"], [(42, "a")]),
                # second insert: straight to the fallback batch, pk cached
                ResultSet(["new_id"], [(43,)]),
                ResultSet(["id", "name"], [(43, "b")]),
            ]
        )
        adapter = make_adapter(MSSQL_CONFIG, cur)

        assert await adapter.insert("users", {"name": "a"}) == {"id": 42, "name": "a"}
        assert "users" in adapter._output_disabled
        batch, values = cur.executed[1]
        assert batch.startswith("SET NOCOUNT ON; INSERT INTO [users] ([name]) VALUES (?); ")
        assert "SCOPE_IDENTITY()" in batch and batch.endswith("SET NOCOUNT OFF;")
        assert values == ["a"]
        assert cur.executed[-1] == ("SELECT * FROM [users] WHERE [id] = ?", [42])

        assert await adapter.insert("users", {"name": "b"}) == {"id": 43, "name": "b"}
        assert not any("OUTPUT" in sql for sql, _ in cur.executed[len(cur.executed) - 2 :])

    @pytest.mark.asyncio
    async def test_mssql_fallback_without_identity_uses_the_given_key(self):
        cur = FakeCursor(
            [
                TRIGGER_ERROR,
                ResultSet(["new_id"], [(None,)]),
                *_schema_results(pk="code"),
                ResultSet(["code"], [("X1",)]),
            ]
        )
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.insert("codes", {"code": "X1"}) == {"code": "X1"}
        assert cur.executed[-1] == ("SELECT * FROM [codes] WHERE [code] = ?", ["X1"])

    @pytest.mark.asyncio
    async def test_mssql_fallback_without_any_key_returns_the_data(self):
        cur = FakeCursor(
            [TRIGGER_ERROR, ResultSet(["new_id"], [(None,)]), *_schema_results(pk=None)]
        )
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.insert("log", {"msg": "x"}) == {"msg": "x"}

    @pytest.mark.asyncio
    async def test_other_errors_propagate(self):
        cur = FakeCursor([RuntimeError("deadlock")])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        with pytest.raises(RuntimeError, match="deadlock"):
            await adapter.insert("users", {"name": "a"})
        assert adapter._output_disabled == set()

    @pytest.mark.asyncio
    async def test_generic_reselects_by_the_provided_key(self):
        cur = FakeCursor(
            [
                NO_RESULT,  # INSERT
                ResultSet(["c"], [self._col("ID")]),  # SQLColumns
                ResultSet(["c"], [(None, "APP", "T", "ID", 1, "PK")]),  # SQLPrimaryKeys
                ResultSet(["c"], []),  # SQLForeignKeys
                ResultSet(["c"], []),  # SQLStatistics
                ResultSet(["ID", "NAME"], [(5, "a")]),
            ]
        )
        adapter = make_adapter(GENERIC_CONFIG, cur)
        assert await adapter.insert("T", {"ID": 5, "NAME": "a"}) == {"ID": 5, "NAME": "a"}
        assert cur.executed[0] == ('INSERT INTO "T" ("ID", "NAME") VALUES (?, ?)', [5, "a"])
        assert cur.executed[1] == ('SELECT * FROM "T" WHERE "ID" = ?', [5])

    @pytest.mark.asyncio
    async def test_generic_without_key_returns_the_data(self):
        cur = FakeCursor([NO_RESULT, *[ResultSet(["c"], []) for _ in range(4)]])
        adapter = make_adapter(GENERIC_CONFIG, cur)
        assert await adapter.insert("T", {"NAME": "a"}) == {"NAME": "a"}
        assert len(cur.executed) == 1

    @staticmethod
    def _col(name: str) -> tuple[Any, ...]:
        return (
            None,
            "APP",
            "T",
            name,
            0,
            "int",
            10,
            None,
            0,
            None,
            0,
            None,
            None,
            None,
            None,
            None,
            1,
            "NO",
        )


class TestSelect:
    @pytest.mark.asyncio
    async def test_count_and_rows(self):
        cur = FakeCursor(
            [ResultSet(["CNT"], [(2,)]), ResultSet(["id", "name"], [(1, "a"), (2, "b")])]
        )
        adapter = make_adapter(MSSQL_CONFIG, cur)
        rows, total = await adapter.select(
            "users",
            ["id", "name"],
            [("name", "like", "%a%")],
            {"limit": 10, "offset": 0},
            [("id", "asc")],
        )
        assert total == 2 and rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
        assert cur.executed[0] == (
            "SELECT COUNT(*) AS cnt FROM [users] WHERE [name] LIKE ?",
            ["%a%"],
        )
        assert cur.executed[1][0].endswith(
            "ORDER BY [id] ASC OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY"
        )

    @pytest.mark.asyncio
    async def test_empty(self):
        cur = FakeCursor([ResultSet(["cnt"], []), ResultSet(["id"], [])])
        assert await make_adapter(MSSQL_CONFIG, cur).select("users") == ([], 0)

    @pytest.mark.asyncio
    async def test_select_by_id(self):
        cur = FakeCursor([ResultSet(["id"], [(1,)]), ResultSet(["id"], [])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.select_by_id("users", "id", 1, ["id"]) == {"id": 1}
        assert await adapter.select_by_id("users", "id", 2) is None
        assert cur.executed[0] == ("SELECT [id] FROM [users] WHERE [id] = ?", [1])


class TestUpdate:
    @pytest.mark.asyncio
    async def test_mssql_output(self):
        cur = FakeCursor([ResultSet(["id", "name"], [(1, "z")]), ResultSet(["id", "name"], [])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.update("users", "id", 1, {"name": "z"}) == {"id": 1, "name": "z"}
        assert await adapter.update("users", "id", 2, {"name": "z"}) is None
        assert cur.executed[0] == (
            "UPDATE [users] SET [name] = ? OUTPUT INSERTED.* WHERE [id] = ?",
            ["z", 1],
        )

    @pytest.mark.asyncio
    async def test_empty_data_reads_the_row(self):
        cur = FakeCursor([ResultSet(["id"], [(1,)])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.update("users", "id", 1, {}) == {"id": 1}
        assert cur.executed[0][0].startswith("SELECT")

    @pytest.mark.asyncio
    async def test_mssql_trigger_fallback_uses_rowcount(self):
        cur = FakeCursor(
            [
                TRIGGER_ERROR,
                ResultSet(rowcount=1),
                ResultSet(["id", "name"], [(1, "z")]),
                ResultSet(rowcount=0),
            ]
        )
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.update("users", "id", 1, {"name": "z"}) == {"id": 1, "name": "z"}
        assert cur.executed[1] == ("UPDATE [users] SET [name] = ? WHERE [id] = ?", ["z", 1])
        assert await adapter.update("users", "id", 9, {"name": "z"}) is None

    @pytest.mark.asyncio
    async def test_generic_unknown_rowcount_rereads(self):
        cur = FakeCursor([ResultSet(rowcount=-1), ResultSet(["ID"], [])])
        adapter = make_adapter(GENERIC_CONFIG, cur)
        assert await adapter.update("T", "ID", 1, {"NAME": "z"}) is None
        assert cur.executed[0] == ('UPDATE "T" SET "NAME" = ? WHERE "ID" = ?', ["z", 1])
        assert cur.executed[1][0] == 'SELECT * FROM "T" WHERE "ID" = ?'


class TestDelete:
    @pytest.mark.asyncio
    async def test_mssql_output(self):
        cur = FakeCursor([ResultSet(["id"], [(1,)]), ResultSet(["id"], [])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.delete("users", "id", 1) is True
        assert await adapter.delete("users", "id", 2) is False
        assert cur.executed[0] == ("DELETE FROM [users] OUTPUT DELETED.[id] WHERE [id] = ?", [1])

    @pytest.mark.asyncio
    async def test_mssql_trigger_fallback_uses_rowcount(self):
        cur = FakeCursor([TRIGGER_ERROR, ResultSet(rowcount=1), ResultSet(rowcount=0)])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        assert await adapter.delete("users", "id", 1) is True
        assert cur.executed[1] == ("DELETE FROM [users] WHERE [id] = ?", [1])
        assert await adapter.delete("users", "id", 2) is False

    @pytest.mark.asyncio
    async def test_generic_checks_existence_first(self):
        cur = FakeCursor([ResultSet(["ID"], []), ResultSet(["ID"], [(1,)]), ResultSet(rowcount=-1)])
        adapter = make_adapter(GENERIC_CONFIG, cur)
        assert await adapter.delete("T", "ID", 9) is False
        assert len(cur.executed) == 1
        assert await adapter.delete("T", "ID", 1) is True
        assert cur.executed[-1] == ('DELETE FROM "T" WHERE "ID" = ?', [1])


class TestSafetyHelpers:
    def test_sanitize_and_where(self):
        adapter = ODBCAdapter(MSSQL_CONFIG)
        assert adapter._sanitize_identifier("user_id") == "user_id"
        with pytest.raises(ValueError):
            adapter._sanitize_identifier("x]; DROP TABLE users; --")
        clause, idx, params = adapter._build_where_clause("age", "gte", 18, 1)
        assert (clause, idx, params) == ("[age] >= ?", 2, [18])


# --- streaming reads and row estimates ----------------------------------------------


class TestStreamSelect:
    @pytest.mark.asyncio
    async def test_rows_arrive_in_batches(self) -> None:
        cur = FakeCursor([ResultSet(["id", "name"], [(1, "a"), (2, "b"), (3, "c")])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        batches = [
            batch
            async for batch in adapter.stream_select(
                "users", ["id", "name"], [("id", "gt", 0)], [("id", "asc")], batch_size=2
            )
        ]
        assert batches == [
            [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
            [{"id": 3, "name": "c"}],
        ]
        sql, params = cur.executed[0]
        assert sql == "SELECT [id], [name] FROM [users] WHERE [id] > ? ORDER BY [id] ASC"
        assert params == [0]

    @pytest.mark.asyncio
    async def test_a_limit_uses_offset_fetch(self) -> None:
        cur = FakeCursor([ResultSet(["id"], [(1,)])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        _ = [batch async for batch in adapter.stream_select("users", limit=10)]
        assert cur.executed[0][0].endswith("OFFSET 0 ROWS FETCH NEXT 10 ROWS ONLY")

    @pytest.mark.asyncio
    async def test_a_statement_without_rows_yields_nothing(self) -> None:
        adapter = make_adapter(MSSQL_CONFIG, FakeCursor([NO_RESULT]))
        assert [batch async for batch in adapter.stream_select("users")] == []

    @pytest.mark.asyncio
    async def test_the_timeout_is_ignored_not_fatal(self) -> None:
        # ODBC has no portable per-statement timeout; the read still works.
        cur = FakeCursor([ResultSet(["id"], [(1,)])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        batches = [b async for b in adapter.stream_select("users", statement_timeout_ms=5000)]
        assert batches == [[{"id": 1}]]


class TestRowEstimates:
    @pytest.mark.asyncio
    async def test_sql_server_reads_partition_counts(self) -> None:
        cur = FakeCursor([ResultSet(["table_name", "row_count"], [("users", 4321)])])
        adapter = make_adapter(MSSQL_CONFIG, cur)
        estimates = await adapter.row_estimates(["users", "orders"])
        assert estimates == {"users": 4321, "orders": None}
        sql, params = cur.executed[0]
        assert "sys.partitions" in sql and "COUNT(*)" not in sql.upper()
        assert params[1:] == ["users", "orders"]

    @pytest.mark.asyncio
    async def test_empty_input_asks_nothing(self) -> None:
        cur = FakeCursor()
        assert await make_adapter(MSSQL_CONFIG, cur).row_estimates([]) == {}
        assert cur.executed == []

    @pytest.mark.asyncio
    async def test_a_generic_odbc_source_reports_nothing(self) -> None:
        cur = FakeCursor()
        adapter = make_adapter({**MSSQL_CONFIG, "type": "odbc"}, cur)
        assert await adapter.row_estimates(["users"]) == {"users": None}
        assert cur.executed == []


# --- Oracle profile -----------------------------------------------------------------

ORACLE_CONFIG: dict[str, Any] = {
    "name": "ora",
    "type": "oracle",
    "host": "ora.example",
    "port": 1521,
    "database": "FREEPDB1",
    "username": "app",
    "password": "s3cret",
    "options": {"driver": "Oracle 23 ODBC driver", "schema": "hr"},
}


class TestOracleProfile:
    def test_schema_is_upper_cased_from_config(self):
        adapter = make_adapter(ORACLE_CONFIG, FakeCursor())
        # ALL_TABLES.OWNER is upper case, so the bound :schema must be too.
        assert adapter._schema == "HR"
        assert adapter._oracle is True
        assert adapter._mssql is False
        assert adapter._resolve_schema is False

    async def test_schema_resolves_from_the_session_when_unset(self):
        config = {**ORACLE_CONFIG, "options": {"driver": "d"}}
        adapter = ODBCAdapter(config)
        assert adapter._resolve_schema is True
        cursor = FakeCursor([ResultSet(["username"], [("APP",)])])
        adapter._pool = FakePool(cursor)

        await adapter._adopt_session_schema()

        assert adapter._schema == "APP"
        assert "FROM DUAL" in cursor.executed[0][0]

    async def test_a_failed_session_lookup_keeps_the_configured_schema(self):
        config = {**ORACLE_CONFIG, "options": {"driver": "d"}}
        adapter = ODBCAdapter(config)
        before = adapter._schema
        adapter._pool = FakePool(FakeCursor([RuntimeError("ORA-01017")]))

        await adapter._adopt_session_schema()

        assert adapter._schema == before

    async def test_get_tables_reads_all_tables(self):
        cursor = FakeCursor([ResultSet(["table_name"], [("ORDERS",), ("USERS",)])])
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        assert await adapter.get_tables() == ["ORDERS", "USERS"]
        sql, params = cursor.executed[0]
        assert "ALL_TABLES" in sql
        assert params == ["HR"]

    async def test_table_schema(self):
        cursor = FakeCursor(
            [
                ResultSet(
                    [
                        "column_name",
                        "data_type",
                        "nullable",
                        "column_default",
                        "max_length",
                        "numeric_precision",
                        "numeric_scale",
                        "is_identity",
                        "is_virtual",
                    ],
                    [
                        ("ID", "NUMBER", "N", None, None, 10, 0, "YES", "NO"),
                        ("EMAIL", "VARCHAR2", "Y", None, 255, None, None, "NO", "NO"),
                        ("TOTAL", "NUMBER", "Y", None, None, None, None, "NO", "YES"),
                    ],
                ),
                ResultSet(["column_name"], [("ID",)]),
                ResultSet(
                    ["column_name", "foreign_table", "foreign_column", "constraint_name"],
                    [("ORG_ID", "ORGS", "ID", "FK_U_ORG")],
                ),
                ResultSet(
                    ["index_name", "column_name", "uniqueness"],
                    [("IX_EMAIL", "EMAIL", "UNIQUE"), ("IX_NAME", "NAME", "NONUNIQUE")],
                ),
            ]
        )
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        schema = await adapter.get_table_schema("users")

        assert schema["table_name"] == "users"
        assert schema["primary_key"] == "ID"
        ident, email, total = schema["columns"]
        assert ident["extra"] == "identity"
        assert ident["nullable"] is False
        assert ident["full_type"] == "number(10,0)"
        assert email["nullable"] is True
        assert email["full_type"] == "varchar2(255)"
        # A NUMBER with no declared precision stays bare: nothing to size it with.
        assert total["full_type"] == "number"
        assert total["extra"] == "computed"
        assert schema["foreign_keys"] == [
            {
                "column": "ORG_ID",
                "references_table": "ORGS",
                "references_column": "ID",
                "constraint_name": "FK_U_ORG",
            }
        ]
        assert schema["indexes"] == [
            {"name": "IX_EMAIL", "columns": ["EMAIL"], "unique": True},
            {"name": "IX_NAME", "columns": ["NAME"], "unique": False},
        ]
        # Every catalog query is scoped by the folded owner and table name.
        for _sql, params in cursor.executed:
            assert params == ["HR", "USERS"]

    async def test_columns_fall_back_to_the_pre_12c_view(self):
        cursor = FakeCursor(
            [
                RuntimeError('ORA-00904: "IDENTITY_COLUMN": invalid identifier'),
                ResultSet(
                    [
                        "column_name",
                        "data_type",
                        "nullable",
                        "column_default",
                        "max_length",
                        "numeric_precision",
                        "numeric_scale",
                        "is_identity",
                        "is_virtual",
                    ],
                    [("ID", "NUMBER", "N", None, None, 10, 0, "NO", "NO")],
                ),
                ResultSet(["column_name"], [("ID",)]),
                ResultSet(
                    ["column_name", "foreign_table", "foreign_column", "constraint_name"], []
                ),
                ResultSet(["index_name", "column_name", "uniqueness"], []),
            ]
        )
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        schema = await adapter.get_table_schema("users")

        assert [c["name"] for c in schema["columns"]] == ["ID"]
        assert schema["columns"][0]["extra"] is None
        assert "ALL_TAB_COLUMNS" in cursor.executed[1][0]

    async def test_another_oracle_error_is_not_swallowed(self):
        cursor = FakeCursor([RuntimeError("ORA-00942: table or view does not exist")])
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        with pytest.raises(RuntimeError, match="ORA-00942"):
            await adapter.get_table_schema("users")

    async def test_row_estimates_map_back_to_the_callers_spelling(self):
        cursor = FakeCursor(
            [ResultSet(["table_name", "row_count"], [("USERS", 1500), ("ORDERS", None)])]
        )
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        # NUM_ROWS is NULL until the table has been analysed.
        assert await adapter.row_estimates(["users", "orders", "missing"]) == {
            "users": 1500,
            "orders": None,
            "missing": None,
        }
        _sql, params = cursor.executed[0]
        assert params == ["HR", "USERS", "ORDERS", "MISSING"]

    async def test_row_estimates_shortcut_on_no_tables(self):
        adapter = make_adapter(ORACLE_CONFIG, FakeCursor())
        assert await adapter.row_estimates([]) == {}


class TestOracleConnectionString:
    """Oracle's driver takes an Easy Connect DBQ, not SQL Server's SERVER/DATABASE."""

    def test_dbq_is_host_port_service(self):
        dsn = build_connection_string(ORACLE_CONFIG)
        assert "DBQ=ora.example:1521/FREEPDB1" in dsn
        # SERVER=host,port reaches Oracle as ORA-12162.
        assert "SERVER=" not in dsn and "DATABASE=" not in dsn
        assert "UID=app" in dsn and "PWD=s3cret" in dsn

    def test_without_a_port(self):
        config = {**ORACLE_CONFIG, "port": None}
        assert "DBQ=ora.example/FREEPDB1" in build_connection_string(config)

    def test_without_a_service(self):
        config = {**ORACLE_CONFIG, "database": ""}
        assert "DBQ=ora.example:1521;" in build_connection_string(config)

    def test_oracle_gets_no_sql_server_encryption_options(self):
        dsn = build_connection_string(ORACLE_CONFIG)
        assert "Encrypt=" not in dsn and "TrustServerCertificate=" not in dsn

    def test_a_verbatim_connection_string_still_wins(self):
        config = {**ORACLE_CONFIG, "options": {"connection_string": "DSN=ora"}}
        assert build_connection_string(config).startswith("DSN=ora;UID=app;")


class TestOracleCatalogNumbers:
    """Oracle reports lengths and precisions as NUMBER, so pyodbc yields floats."""

    @pytest.mark.parametrize(
        ("raw", "expected"), [(50.0, 50), (10, 10), ("7", 7), (None, None), (True, None)]
    )
    def test_as_int(self, raw, expected):
        assert odbc_module._as_int(raw) == expected

    async def test_a_float_length_does_not_leak_into_the_type_name(self):
        cursor = FakeCursor(
            [
                ResultSet(
                    [
                        "column_name",
                        "data_type",
                        "nullable",
                        "column_default",
                        "max_length",
                        "numeric_precision",
                        "numeric_scale",
                        "is_identity",
                        "is_virtual",
                    ],
                    [
                        ("USERNAME", "VARCHAR2", "N", None, 50.0, None, None, "NO", "NO"),
                        ("TOTAL", "NUMBER", "Y", None, None, 10.0, 2.0, "NO", "NO"),
                    ],
                ),
                ResultSet(["column_name"], []),
                ResultSet(
                    ["column_name", "foreign_table", "foreign_column", "constraint_name"], []
                ),
                ResultSet(["index_name", "column_name", "uniqueness"], []),
            ]
        )
        schema = await make_adapter(ORACLE_CONFIG, cursor).get_table_schema("users")
        username, total = schema["columns"]
        assert username["full_type"] == "varchar2(50)"
        assert username["max_length"] == 50
        assert total["full_type"] == "number(10,2)"
        # pa.decimal128 will not take a float.
        assert (total["precision"], total["scale"]) == (10, 2)


class TestOracleIdentityInsert:
    """Oracle has no RETURNING here, so the generated key comes from the sequence."""

    async def test_the_new_row_is_refetched_by_the_sequence_value(self):
        cursor = FakeCursor(
            [
                ResultSet(  # identity lookup
                    ["column_name", "sequence_name"], [("ID", "ISEQ$$_73346")]
                ),
                NO_RESULT,  # the INSERT
                ResultSet(["CURRVAL"], [(7,)]),  # CURRVAL, same connection
                ResultSet(  # _primary_key -> get_table_schema
                    [
                        "column_name",
                        "data_type",
                        "nullable",
                        "column_default",
                        "max_length",
                        "numeric_precision",
                        "numeric_scale",
                        "is_identity",
                        "is_virtual",
                    ],
                    [("ID", "NUMBER", "N", None, None, None, None, "YES", "NO")],
                ),
                ResultSet(["column_name"], [("ID",)]),
                ResultSet(
                    ["column_name", "foreign_table", "foreign_column", "constraint_name"], []
                ),
                ResultSet(["index_name", "column_name", "uniqueness"], []),
                ResultSet(["ID", "USERNAME"], [(7, "dave")]),  # the re-select
            ]
        )
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        created = await adapter.insert("users", {"username": "dave"})

        assert created == {"ID": 7, "USERNAME": "dave"}
        # The sequence name contains '$', which only Oracle's identifier rules allow.
        assert any('"ISEQ$$_73346".CURRVAL' in sql for sql, _ in cursor.executed)

    async def test_a_table_without_an_identity_column_falls_back_to_the_given_key(self):
        cursor = FakeCursor(
            [
                ResultSet(["column_name", "sequence_name"], []),  # no identity
                NO_RESULT,  # the INSERT
                ResultSet(  # _primary_key
                    [
                        "column_name",
                        "data_type",
                        "nullable",
                        "column_default",
                        "max_length",
                        "numeric_precision",
                        "numeric_scale",
                        "is_identity",
                        "is_virtual",
                    ],
                    [("ID", "NUMBER", "N", None, None, None, None, "NO", "NO")],
                ),
                ResultSet(["column_name"], [("ID",)]),
                ResultSet(
                    ["column_name", "foreign_table", "foreign_column", "constraint_name"], []
                ),
                ResultSet(["index_name", "column_name", "uniqueness"], []),
                ResultSet(["ID", "USERNAME"], [(3, "erin")]),
            ]
        )
        adapter = make_adapter(ORACLE_CONFIG, cursor)

        created = await adapter.insert("users", {"ID": 3, "username": "erin"})

        assert created == {"ID": 3, "USERNAME": "erin"}
        assert not any("CURRVAL" in sql for sql, _ in cursor.executed)

    async def test_the_sequence_lookup_is_cached(self):
        cursor = FakeCursor([ResultSet(["column_name", "sequence_name"], [("ID", "S1")])])
        adapter = make_adapter(ORACLE_CONFIG, cursor)
        assert await adapter._oracle_identity_sequence("users") == "S1"
        assert await adapter._oracle_identity_sequence("users") == "S1"
        assert len(cursor.executed) == 1


class TestOracleInexactNumbers:
    """An undeclared NUMBER cannot round-trip, and the fix belongs in the DDL."""

    def _columns(self, *specs):
        return ResultSet(
            [
                "column_name",
                "data_type",
                "nullable",
                "column_default",
                "max_length",
                "numeric_precision",
                "numeric_scale",
                "is_identity",
                "is_virtual",
            ],
            [
                (name, dtype, "Y", None, None, precision, scale, "NO", "NO")
                for name, dtype, precision, scale in specs
            ],
        )

    def _cursor(self, *specs):
        return FakeCursor(
            [
                self._columns(*specs),
                ResultSet(["column_name"], []),
                ResultSet(
                    ["column_name", "foreign_table", "foreign_column", "constraint_name"], []
                ),
                ResultSet(["index_name", "column_name", "uniqueness"], []),
            ]
        )

    async def test_an_undeclared_number_is_flagged(self):
        cursor = self._cursor(("ID", "NUMBER", None, None), ("TOTAL", "NUMBER", 10, 2))
        schema = await make_adapter(ORACLE_CONFIG, cursor).get_table_schema("users")
        flags = {c["name"]: c["inexact"] for c in schema["columns"]}
        assert flags == {"ID": True, "TOTAL": False}

    async def test_a_declared_precision_is_exact(self):
        # Proven against a real Oracle: NUMBER(38) arrives as an exact Decimal
        # while a bare NUMBER arrives as an IEEE double.
        cursor = self._cursor(("BIG_ID", "NUMBER", 38, 0))
        schema = await make_adapter(ORACLE_CONFIG, cursor).get_table_schema("t")
        assert schema["columns"][0]["inexact"] is False

    async def test_other_numeric_types_are_not_flagged(self):
        cursor = self._cursor(("F", "BINARY_DOUBLE", None, None), ("S", "VARCHAR2", None, None))
        schema = await make_adapter(ORACLE_CONFIG, cursor).get_table_schema("t")
        assert not any(c["inexact"] for c in schema["columns"])

    async def test_the_warning_names_the_column_and_the_fix(self, caplog):
        cursor = self._cursor(("ID", "NUMBER", None, None))
        with caplog.at_level(logging.WARNING):
            await make_adapter(ORACLE_CONFIG, cursor).get_table_schema("users")
        assert "ID" in caplog.text
        assert "2^53" in caplog.text
        assert "NUMBER(38)" in caplog.text

    async def test_it_warns_once_per_table_not_once_per_query(self, caplog):
        adapter = make_adapter(ORACLE_CONFIG, self._cursor(("ID", "NUMBER", None, None)))
        with caplog.at_level(logging.WARNING):
            await adapter.get_table_schema("users")
            adapter._pool = FakePool(self._cursor(("ID", "NUMBER", None, None)))
            await adapter.get_table_schema("users")
        assert caplog.text.count("NUMBER without a precision") == 1
