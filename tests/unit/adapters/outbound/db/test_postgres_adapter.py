"""Unit tests for PostgreSQLAdapter using a fake asyncpg connection pool.

No real database: the pool's acquire() returns an async-context-manager wrapping
a fake connection whose fetch/fetchrow/fetchval are AsyncMocks returning canned
rows. Asserts rows are mapped correctly and SQL is built as expected.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from warp.adapters.outbound.db.postgres import PostgreSQLAdapter

CONFIG: dict[str, Any] = {
    "name": "pg",
    "type": "postgresql",
    "host": "localhost",
    "port": 5432,
    "database": "testdb",
    "username": "u",
    "password": "p",
}


class FakeAcquire:
    """Async context manager returned by pool.acquire()."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakePool:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self.closed = False

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self._conn)

    async def close(self) -> None:
        self.closed = True


def make_adapter(conn: Any) -> PostgreSQLAdapter:
    adapter = PostgreSQLAdapter(CONFIG)
    adapter._pool = FakePool(conn)
    return adapter


@pytest.fixture
def conn() -> AsyncMock:
    c = AsyncMock()
    return c


@pytest.mark.asyncio
async def test_get_tables(conn: AsyncMock) -> None:
    conn.fetch.return_value = [{"table_name": "users"}, {"table_name": "orders"}]
    adapter = make_adapter(conn)
    tables = await adapter.get_tables()
    assert tables == ["users", "orders"]


@pytest.mark.asyncio
async def test_get_table_schema(conn: AsyncMock) -> None:
    columns = [
        {
            "column_name": "id",
            "data_type": "integer",
            "udt_name": "int4",
            "is_nullable": "NO",
            "column_default": "nextval(...)",
            "character_maximum_length": None,
            "numeric_precision": 32,
            "numeric_scale": 0,
        },
        {
            "column_name": "email",
            "data_type": "character varying",
            "udt_name": "varchar",
            "is_nullable": "YES",
            "column_default": None,
            "character_maximum_length": 255,
            "numeric_precision": None,
            "numeric_scale": None,
        },
    ]
    pk = [{"column_name": "id"}]
    fks = [
        {
            "column_name": "user_id",
            "foreign_table": "users",
            "foreign_column": "id",
            "constraint_name": "fk_user",
        }
    ]
    idx = [
        {"index_name": "ix_email", "column_name": "email", "is_unique": True, "is_primary": False},
        {"index_name": "ix_email", "column_name": "domain", "is_unique": True, "is_primary": False},
    ]
    # Order of fetch calls: columns, pk, fks, indexes
    conn.fetch.side_effect = [columns, pk, fks, idx]

    adapter = make_adapter(conn)
    schema = await adapter.get_table_schema("accounts")

    assert schema["table_name"] == "accounts"
    assert len(schema["columns"]) == 2
    assert schema["columns"][0]["name"] == "id"
    assert schema["columns"][0]["nullable"] is False
    assert schema["columns"][0]["extra"] is None
    assert schema["columns"][1]["nullable"] is True
    assert schema["primary_key"] == "id"
    assert schema["foreign_keys"][0]["references_table"] == "users"
    assert len(schema["indexes"]) == 1
    assert schema["indexes"][0]["columns"] == ["email", "domain"]
    assert schema["indexes"][0]["unique"] is True


@pytest.mark.asyncio
async def test_get_table_schema_composite_pk(conn: AsyncMock) -> None:
    conn.fetch.side_effect = [
        [],  # no columns
        [{"column_name": "a"}, {"column_name": "b"}],  # composite pk
        [],  # no fks
        [],  # no indexes
    ]
    adapter = make_adapter(conn)
    schema = await adapter.get_table_schema("t")
    assert schema["primary_key"] == ["a", "b"]


@pytest.mark.asyncio
async def test_execute_query_no_params(conn: AsyncMock) -> None:
    conn.fetch.return_value = [{"n": 1}, {"n": 2}]
    adapter = make_adapter(conn)
    rows = await adapter.execute_query("SELECT n FROM t")
    assert rows == [{"n": 1}, {"n": 2}]


@pytest.mark.asyncio
async def test_execute_query_named_params(conn: AsyncMock) -> None:
    conn.fetch.return_value = [{"id": 5}]
    adapter = make_adapter(conn)
    rows = await adapter.execute_query(
        "SELECT * FROM t WHERE id = :id AND s = :s",
        params={"id": 5, "s": "x"},
    )
    assert rows == [{"id": 5}]
    sent_query = conn.fetch.call_args.args[0]
    assert "$1" in sent_query and "$2" in sent_query
    assert ":id" not in sent_query


@pytest.mark.asyncio
async def test_insert(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": 1, "name": "alice"}
    adapter = make_adapter(conn)
    result = await adapter.insert("users", {"name": "alice"})
    assert result == {"id": 1, "name": "alice"}
    query = conn.fetchrow.call_args.args[0]
    assert "INSERT INTO" in query and "RETURNING" in query


@pytest.mark.asyncio
async def test_insert_no_row(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = None
    adapter = make_adapter(conn)
    result = await adapter.insert("users", {"name": "alice"})
    assert result == {}


@pytest.mark.asyncio
async def test_select_basic(conn: AsyncMock) -> None:
    conn.fetchval.return_value = 2
    conn.fetch.return_value = [{"id": 1}, {"id": 2}]
    adapter = make_adapter(conn)
    rows, total = await adapter.select("users")
    assert total == 2
    assert rows == [{"id": 1}, {"id": 2}]


@pytest.mark.asyncio
async def test_select_with_filters_sort_pagination(conn: AsyncMock) -> None:
    conn.fetchval.return_value = 1
    conn.fetch.return_value = [{"id": 1, "status": "active"}]
    adapter = make_adapter(conn)
    rows, total = await adapter.select(
        "users",
        columns=["id", "status"],
        filters=[("status", "eq", "active"), ("id", "gte", 1)],
        pagination={"limit": 10, "offset": 0},
        sort=[("id", "desc")],
    )
    assert total == 1
    assert rows[0]["status"] == "active"
    query = conn.fetch.call_args.args[0]
    assert "WHERE" in query and "ORDER BY" in query and "LIMIT" in query


@pytest.mark.asyncio
async def test_select_in_and_isnull_filters(conn: AsyncMock) -> None:
    conn.fetchval.return_value = 0
    conn.fetch.return_value = []
    adapter = make_adapter(conn)
    rows, total = await adapter.select(
        "users",
        filters=[("id", "in", [1, 2, 3]), ("deleted_at", "is_null", True)],
    )
    assert total == 0
    assert rows == []
    count_query = conn.fetchval.call_args.args[0]
    assert "IN (" in count_query
    assert "IS NULL" in count_query


@pytest.mark.asyncio
async def test_select_by_id_found(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": 7, "name": "x"}
    adapter = make_adapter(conn)
    result = await adapter.select_by_id("users", {"id": 7})
    assert result == {"id": 7, "name": "x"}


@pytest.mark.asyncio
async def test_select_by_id_not_found(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = None
    adapter = make_adapter(conn)
    result = await adapter.select_by_id("users", {"id": 99}, columns=["id"])
    assert result is None


@pytest.mark.asyncio
async def test_update(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": 1, "name": "new"}
    adapter = make_adapter(conn)
    result = await adapter.update("users", {"id": 1}, {"name": "new"})
    assert result == {"id": 1, "name": "new"}
    query = conn.fetchrow.call_args.args[0]
    assert "UPDATE" in query and "SET" in query


@pytest.mark.asyncio
async def test_update_empty_data_delegates(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": 1}
    adapter = make_adapter(conn)
    result = await adapter.update("users", {"id": 1}, {})
    assert result == {"id": 1}


@pytest.mark.asyncio
async def test_delete_true(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = {"id": 1}
    adapter = make_adapter(conn)
    assert await adapter.delete("users", {"id": 1}) is True


@pytest.mark.asyncio
async def test_delete_false(conn: AsyncMock) -> None:
    conn.fetchrow.return_value = None
    adapter = make_adapter(conn)
    assert await adapter.delete("users", {"id": 999}) is False


@pytest.mark.asyncio
async def test_disconnect_closes_pool(conn: AsyncMock) -> None:
    adapter = make_adapter(conn)
    pool = adapter._pool
    await adapter.disconnect()
    assert pool.closed is True
    assert adapter._pool is None


def test_build_where_clause_operators() -> None:
    adapter = PostgreSQLAdapter(CONFIG)
    for op, expected in [
        ("eq", "="),
        ("ne", "!="),
        ("gt", ">"),
        ("gte", ">="),
        ("lt", "<"),
        ("lte", "<="),
        ("like", "ILIKE"),
    ]:
        clause, idx, params = adapter._build_where_clause("c", op, 1, 1)
        assert expected in clause
        assert params == [1]
        assert idx == 2


def test_build_where_clause_in_scalar() -> None:
    adapter = PostgreSQLAdapter(CONFIG)
    clause, idx, params = adapter._build_where_clause("c", "in", 5, 1)
    assert "=" in clause
    assert params == [5]


def test_build_where_clause_unknown_op_defaults_eq() -> None:
    adapter = PostgreSQLAdapter(CONFIG)
    clause, idx, params = adapter._build_where_clause("c", "weird", 5, 1)
    assert "=" in clause
    assert params == [5]


@pytest.mark.asyncio
async def test_execute_query_repeated_param_and_cast(conn: AsyncMock) -> None:
    conn.fetch.return_value = []
    adapter = make_adapter(conn)
    await adapter.execute_query("SELECT * FROM t WHERE a = :v::int OR b = :v", params={"v": 5})
    sent_query, *sent_args = conn.fetch.call_args.args
    assert sent_query == "SELECT * FROM t WHERE a = $1::int OR b = $1"
    assert sent_args == [5]


@pytest.mark.asyncio
async def test_execute_query_missing_param_raises_value_error(conn: AsyncMock) -> None:
    adapter = make_adapter(conn)
    with pytest.raises(ValueError, match="Missing value"):
        await adapter.execute_query("SELECT :a", params={"b": 1})
    conn.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_row_estimates_from_pg_class(conn: AsyncMock) -> None:
    conn.fetch.return_value = [
        {"table_name": "users", "estimate": 1234.0},
        {"table_name": "never_analyzed", "estimate": -1.0},
    ]
    adapter = make_adapter(conn)
    estimates = await adapter.row_estimates(["users", "never_analyzed", "missing"])
    assert estimates == {"users": 1234, "never_analyzed": None, "missing": None}
    sql, schema, tables = conn.fetch.call_args.args
    assert "pg_class" in sql and "COUNT(*)" not in sql.upper()
    assert schema == "public"
    assert tables == ["users", "never_analyzed", "missing"]


@pytest.mark.asyncio
async def test_row_estimates_empty_input_skips_query(conn: AsyncMock) -> None:
    adapter = make_adapter(conn)
    assert await adapter.row_estimates([]) == {}
    conn.fetch.assert_not_called()


class FakeTransaction:
    """Async context manager standing in for asyncpg's ``conn.transaction()``."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> "FakeTransaction":
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.exited = True


def _cursor_conn(batches: list[list[dict[str, Any]]]) -> tuple[AsyncMock, Any, FakeTransaction]:
    conn = AsyncMock()
    tx = FakeTransaction()
    conn.transaction = lambda: tx  # sync call returning the context manager
    cursor = AsyncMock()
    cursor.fetch = AsyncMock(side_effect=[*batches, []])
    conn.cursor = AsyncMock(return_value=cursor)
    return conn, cursor, tx


@pytest.mark.asyncio
async def test_stream_select_batches_via_server_side_cursor() -> None:
    conn, cursor, tx = _cursor_conn([[{"id": 1}, {"id": 2}], [{"id": 3}]])
    adapter = make_adapter(conn)
    batches = [
        b
        async for b in adapter.stream_select(
            "users", ["id"], [("id", "gt", 0)], [("id", "asc")], batch_size=2, limit=3
        )
    ]
    assert batches == [[{"id": 1}, {"id": 2}], [{"id": 3}]]
    conn.cursor.assert_awaited_once_with(
        'SELECT "id" FROM "users" WHERE "id" > $1 ORDER BY "id" ASC LIMIT 3 OFFSET 0', 0
    )
    assert cursor.fetch.await_args_list[0].args == (2,)
    assert cursor.fetch.await_count == 3  # two batches + the empty one that ends the loop
    assert tx.entered and tx.exited
    conn.execute.assert_not_awaited()  # no statement timeout by default


@pytest.mark.asyncio
async def test_stream_select_sets_local_statement_timeout() -> None:
    conn, _, _ = _cursor_conn([[{"id": 1}]])
    adapter = make_adapter(conn)
    _ = [b async for b in adapter.stream_select("users", statement_timeout_ms=1500)]
    conn.execute.assert_awaited_once_with("SET LOCAL statement_timeout = 1500")


@pytest.mark.asyncio
async def test_stream_select_empty_result_yields_nothing() -> None:
    conn, _, tx = _cursor_conn([])
    adapter = make_adapter(conn)
    assert [b async for b in adapter.stream_select("users")] == []
    assert tx.exited


@pytest.mark.asyncio
async def test_stream_select_early_close_leaves_transaction() -> None:
    conn, cursor, tx = _cursor_conn([[{"id": 1}], [{"id": 2}]])
    adapter = make_adapter(conn)
    stream = adapter.stream_select("users", batch_size=1)
    assert await stream.__anext__() == [{"id": 1}]
    await stream.aclose()
    assert tx.exited and cursor.fetch.await_count == 1


@pytest.mark.asyncio
async def test_get_table_schema_marks_identity_columns(conn: AsyncMock) -> None:
    columns = [
        {
            "column_name": "id",
            "data_type": "bigint",
            "udt_name": "int8",
            "is_nullable": "NO",
            "column_default": None,
            "character_maximum_length": None,
            "numeric_precision": 64,
            "numeric_scale": 0,
            "is_identity": "YES",
        }
    ]
    conn.fetch.side_effect = [columns, [{"column_name": "id"}], [], []]
    schema = await make_adapter(conn).get_table_schema("t")
    assert schema["columns"][0]["extra"] == "identity"
    assert "is_identity" in conn.fetch.call_args_list[0].args[0]


class TestConfiguredSchema:
    """`options.schema` used to be ignored here, while every other reader honoured it."""

    def test_defaults_to_public(self) -> None:
        assert PostgreSQLAdapter(CONFIG)._schema == "public"

    def test_options_schema_wins(self) -> None:
        config = {**CONFIG, "options": {"schema": "reporting"}}
        assert PostgreSQLAdapter(config)._schema == "reporting"

    @pytest.mark.asyncio
    async def test_every_introspection_query_is_scoped(self, conn: AsyncMock) -> None:
        config = {**CONFIG, "options": {"schema": "reporting"}}
        adapter = PostgreSQLAdapter(config)
        adapter._pool = FakePool(conn)
        conn.fetch.return_value = []

        await adapter.get_tables()
        await adapter.get_table_schema("users")
        await adapter.row_estimates(["users"])

        assert conn.fetch.call_count == 6
        for call in conn.fetch.call_args_list:
            sql, *args = call.args
            assert "'public'" not in sql
            assert args[0] == "reporting"

    @pytest.mark.asyncio
    async def test_indexes_are_scoped_by_schema(self, conn: AsyncMock) -> None:
        # The index query joined nothing to pg_namespace, so a same-named table
        # in another schema contributed its indexes.
        adapter = make_adapter(conn)
        conn.fetch.return_value = []

        await adapter.get_table_schema("users")

        idx_sql = conn.fetch.call_args_list[3].args[0]
        assert "pg_namespace" in idx_sql
        assert "n.nspname = $1" in idx_sql
