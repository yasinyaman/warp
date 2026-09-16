"""Unit tests for MySQLAdapter using a fake aiomysql connection pool.

The MySQL adapter uses `async with pool.acquire() as conn, conn.cursor(...) as
cur`. The fakes below provide both layers as async context managers, with the
cursor's execute/fetchall/fetchone/lastrowid/rowcount controllable per test.
"""

from typing import Any

import aiomysql
import pytest
from pymysql.constants import CLIENT

from warp.database.mysql import MySQLAdapter

CONFIG: dict[str, Any] = {
    "name": "my",
    "type": "mysql",
    "host": "localhost",
    "port": 3306,
    "database": "testdb",
    "username": "u",
    "password": "p",
}


class FakeCursor:
    def __init__(
        self,
        fetchall: list[dict[str, Any]] | None = None,
        fetchone: dict[str, Any] | None = None,
        lastrowid: int | None = None,
        rowcount: int = 0,
        fetchall_seq: list[list[dict[str, Any]]] | None = None,
        fetchone_seq: list[dict[str, Any] | None] | None = None,
    ) -> None:
        self._fetchall = fetchall or []
        self._fetchone = fetchone
        self.lastrowid = lastrowid
        self.rowcount = rowcount
        self._fetchall_seq = fetchall_seq
        self._fetchone_seq = fetchone_seq
        self.executed: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def execute(self, query: str, params: Any = None) -> None:
        self.executed.append((query, params))

    async def fetchall(self) -> list[dict[str, Any]]:
        if self._fetchall_seq is not None:
            return self._fetchall_seq.pop(0)
        return self._fetchall

    async def fetchone(self) -> dict[str, Any] | None:
        if self._fetchone_seq is not None:
            return self._fetchone_seq.pop(0)
        return self._fetchone


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    async def __aenter__(self) -> "FakeConn":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        return self._cursor


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self.closed = False
        self.waited = False

    def acquire(self) -> FakeConn:
        return self._conn

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


def make_adapter(cursor: FakeCursor) -> MySQLAdapter:
    adapter = MySQLAdapter(CONFIG)
    adapter._pool = FakePool(FakeConn(cursor))
    return adapter


@pytest.mark.asyncio
async def test_get_tables() -> None:
    cur = FakeCursor(fetchall=[{"TABLE_NAME": "users"}, {"TABLE_NAME": "orders"}])
    adapter = make_adapter(cur)
    assert await adapter.get_tables() == ["users", "orders"]


@pytest.mark.asyncio
async def test_get_tables_selects_the_key_it_reads() -> None:
    """MariaDB/MySQL 5.7 name result columns as written in the SELECT list; the
    adapter must select exactly the key it later reads (upper-case)."""
    import re

    cur = FakeCursor(fetchall=[{"TABLE_NAME": "users"}])
    adapter = make_adapter(cur)
    await adapter.get_tables()
    query, params = cur.executed[0]
    selected = re.search(r"SELECT\s+(\w+)", query)
    assert selected is not None and selected.group(1) == "TABLE_NAME"
    assert params == ("testdb",)


class _CapturePool(FakePool):
    pass


@pytest.mark.asyncio
async def test_connect_requests_found_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> FakePool:
        captured.update(kwargs)
        return _CapturePool(FakeConn(FakeCursor()))

    monkeypatch.setattr(aiomysql, "create_pool", fake_create_pool)
    adapter = MySQLAdapter({**CONFIG, "options": {"pool_size": 3, "pool_min_size": 1}})
    await adapter.connect()

    assert captured["client_flag"] & CLIENT.FOUND_ROWS
    assert captured["autocommit"] is True
    assert (captured["minsize"], captured["maxsize"]) == (1, 3)
    assert captured["db"] == "testdb" and captured["user"] == "u"
    assert adapter.is_connected


@pytest.mark.asyncio
async def test_get_table_schema() -> None:
    columns = [
        {
            "COLUMN_NAME": "id",
            "DATA_TYPE": "int",
            "COLUMN_TYPE": "int(11)",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": None,
            "CHARACTER_MAXIMUM_LENGTH": None,
            "NUMERIC_PRECISION": 10,
            "NUMERIC_SCALE": 0,
            "COLUMN_KEY": "PRI",
            "EXTRA": "auto_increment",
        },
        {
            "COLUMN_NAME": "email",
            "DATA_TYPE": "varchar",
            "COLUMN_TYPE": "varchar(255)",
            "IS_NULLABLE": "YES",
            "COLUMN_DEFAULT": None,
            "CHARACTER_MAXIMUM_LENGTH": 255,
            "NUMERIC_PRECISION": None,
            "NUMERIC_SCALE": None,
            "COLUMN_KEY": "",
            "EXTRA": "",
        },
    ]
    fks = [
        {
            "COLUMN_NAME": "org_id",
            "REFERENCED_TABLE_NAME": "orgs",
            "REFERENCED_COLUMN_NAME": "id",
            "CONSTRAINT_NAME": "fk_org",
        }
    ]
    idx = [
        {"INDEX_NAME": "ix_email", "COLUMN_NAME": "email", "NON_UNIQUE": 0, "SEQ_IN_INDEX": 1},
    ]
    cur = FakeCursor(fetchall_seq=[columns, fks, idx])
    adapter = make_adapter(cur)
    schema = await adapter.get_table_schema("users")
    assert schema["primary_key"] == "id"
    assert len(schema["columns"]) == 2
    assert schema["foreign_keys"][0]["references_table"] == "orgs"
    assert schema["indexes"][0]["unique"] is True
    assert schema["indexes"][0]["columns"] == ["email"]


@pytest.mark.asyncio
async def test_get_table_schema_composite_pk() -> None:
    columns = [
        {
            "COLUMN_NAME": "a",
            "DATA_TYPE": "int",
            "COLUMN_TYPE": "int",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": None,
            "CHARACTER_MAXIMUM_LENGTH": None,
            "NUMERIC_PRECISION": 10,
            "NUMERIC_SCALE": 0,
            "COLUMN_KEY": "PRI",
            "EXTRA": "",
        },
        {
            "COLUMN_NAME": "b",
            "DATA_TYPE": "int",
            "COLUMN_TYPE": "int",
            "IS_NULLABLE": "NO",
            "COLUMN_DEFAULT": None,
            "CHARACTER_MAXIMUM_LENGTH": None,
            "NUMERIC_PRECISION": 10,
            "NUMERIC_SCALE": 0,
            "COLUMN_KEY": "PRI",
            "EXTRA": "",
        },
    ]
    cur = FakeCursor(fetchall_seq=[columns, [], []])
    adapter = make_adapter(cur)
    schema = await adapter.get_table_schema("t")
    assert schema["primary_key"] == ["a", "b"]


@pytest.mark.asyncio
async def test_execute_query_no_params() -> None:
    cur = FakeCursor(fetchall=[{"n": 1}])
    adapter = make_adapter(cur)
    assert await adapter.execute_query("SELECT 1") == [{"n": 1}]


@pytest.mark.asyncio
async def test_execute_query_named_params() -> None:
    cur = FakeCursor(fetchall=[{"id": 3}])
    adapter = make_adapter(cur)
    rows = await adapter.execute_query("SELECT * FROM t WHERE id = :id", params={"id": 3})
    assert rows == [{"id": 3}]
    query, params = cur.executed[0]
    assert "%s" in query and ":id" not in query
    assert params == [3]


@pytest.mark.asyncio
async def test_insert_with_lastrowid() -> None:
    cur = FakeCursor(fetchone=({"id": 1, "name": "alice"}), lastrowid=1)
    adapter = make_adapter(cur)
    result = await adapter.insert("users", {"name": "alice"})
    assert result == {"id": 1, "name": "alice"}


@pytest.mark.asyncio
async def test_insert_no_lastrowid_returns_data() -> None:
    cur = FakeCursor(lastrowid=0)
    adapter = make_adapter(cur)
    data = {"name": "x"}
    result = await adapter.insert("users", data)
    assert result == data


@pytest.mark.asyncio
async def test_select_basic() -> None:
    cur = FakeCursor(
        fetchall=[{"id": 1}, {"id": 2}],
        fetchone={"cnt": 2},
    )
    adapter = make_adapter(cur)
    rows, total = await adapter.select("users")
    assert total == 2
    assert rows == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_select_with_filters_sort_pagination() -> None:
    cur = FakeCursor(
        fetchone_seq=[{"cnt": 1}],
        fetchall_seq=[[{"id": 1}]],
    )
    adapter = make_adapter(cur)
    rows, total = await adapter.select(
        "users",
        columns=["id"],
        filters=[("status", "eq", "active"), ("id", "in", [1, 2]), ("x", "is_null", False)],
        pagination={"limit": 5, "offset": 5},
        sort=[("id", "asc")],
    )
    assert total == 1
    assert rows == [{"id": 1}]
    # last execute is the main query
    main_query = cur.executed[-1][0]
    assert "WHERE" in main_query and "ORDER BY" in main_query and "LIMIT" in main_query


@pytest.mark.asyncio
async def test_select_count_none() -> None:
    cur = FakeCursor(fetchall=[], fetchone=None)
    adapter = make_adapter(cur)
    rows, total = await adapter.select("users")
    assert total == 0
    assert rows == []


@pytest.mark.asyncio
async def test_select_by_id() -> None:
    cur = FakeCursor(fetchone={"id": 9})
    adapter = make_adapter(cur)
    assert await adapter.select_by_id("users", "id", 9, columns=["id"]) == {"id": 9}


@pytest.mark.asyncio
async def test_update_success() -> None:
    cur = FakeCursor(fetchone={"id": 1, "name": "new"}, rowcount=1)
    adapter = make_adapter(cur)
    result = await adapter.update("users", "id", 1, {"name": "new"})
    assert result == {"id": 1, "name": "new"}


@pytest.mark.asyncio
async def test_update_no_rows() -> None:
    cur = FakeCursor(rowcount=0)
    adapter = make_adapter(cur)
    assert await adapter.update("users", "id", 1, {"name": "x"}) is None


@pytest.mark.asyncio
async def test_update_unchanged_values_returns_row() -> None:
    """With CLIENT.FOUND_ROWS the server reports matched rows (1) even when the
    new values equal the current ones, so the record is returned, not None."""
    cur = FakeCursor(fetchone={"id": 1, "name": "same"}, rowcount=1)
    adapter = make_adapter(cur)
    assert await adapter.update("users", "id", 1, {"name": "same"}) == {"id": 1, "name": "same"}


@pytest.mark.asyncio
async def test_update_empty_data_delegates() -> None:
    cur = FakeCursor(fetchone={"id": 1})
    adapter = make_adapter(cur)
    assert await adapter.update("users", "id", 1, {}) == {"id": 1}


@pytest.mark.asyncio
async def test_delete_true() -> None:
    cur = FakeCursor(rowcount=1)
    adapter = make_adapter(cur)
    assert await adapter.delete("users", "id", 1) is True


@pytest.mark.asyncio
async def test_delete_false() -> None:
    cur = FakeCursor(rowcount=0)
    adapter = make_adapter(cur)
    assert await adapter.delete("users", "id", 1) is False


@pytest.mark.asyncio
async def test_disconnect() -> None:
    cur = FakeCursor()
    adapter = make_adapter(cur)
    pool = adapter._pool
    await adapter.disconnect()
    assert pool.closed is True
    assert pool.waited is True
    assert adapter._pool is None


def test_build_where_clause_operators() -> None:
    adapter = MySQLAdapter(CONFIG)
    for op, expected in [
        ("eq", "="),
        ("ne", "!="),
        ("gt", ">"),
        ("gte", ">="),
        ("lt", "<"),
        ("lte", "<="),
        ("like", "LIKE"),
    ]:
        clause, params = adapter._build_where_clause("c", op, 1)
        assert expected in clause
        assert params == [1]


def test_build_where_clause_in_list_and_scalar() -> None:
    adapter = MySQLAdapter(CONFIG)
    clause, params = adapter._build_where_clause("c", "in", [1, 2])
    assert "IN (" in clause
    assert params == [1, 2]
    clause2, params2 = adapter._build_where_clause("c", "in", 5)
    assert "=" in clause2
    assert params2 == [5]


def test_build_where_clause_is_null() -> None:
    adapter = MySQLAdapter(CONFIG)
    clause_true, params_true = adapter._build_where_clause("c", "is_null", True)
    assert "IS NULL" in clause_true
    assert params_true == []
    clause_false, _ = adapter._build_where_clause("c", "is_null", False)
    assert "IS NOT NULL" in clause_false


def test_build_where_clause_default() -> None:
    adapter = MySQLAdapter(CONFIG)
    clause, params = adapter._build_where_clause("c", "unknown", 7)
    assert "=" in clause
    assert params == [7]


@pytest.mark.asyncio
async def test_execute_query_repeated_param_and_percent() -> None:
    cur = FakeCursor(fetchall=[])
    adapter = make_adapter(cur)
    await adapter.execute_query(
        "SELECT * FROM t WHERE a = :v OR b = :v AND c LIKE 'x%'", params={"v": 5}
    )
    query, params = cur.executed[0]
    assert query == "SELECT * FROM t WHERE a = %s OR b = %s AND c LIKE 'x%%'"
    assert params == [5, 5]
