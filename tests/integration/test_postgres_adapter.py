"""PostgreSQL adapter against a real server."""

import pytest

from warp.application.ports.database import DatabaseGateway

pytestmark = pytest.mark.integration


async def test_introspection(pg_gateway: DatabaseGateway) -> None:
    assert "users" in await pg_gateway.get_tables()
    schema = await pg_gateway.get_table_schema("users")
    names = [c["name"] for c in schema["columns"]]
    assert names[:4] == ["id", "username", "zip", "active"]
    assert schema["primary_key"] == "id"


async def test_crud_round_trip(pg_gateway: DatabaseGateway) -> None:
    rows, total = await pg_gateway.select("users", sort=[("id", "asc")])
    assert total == 3 and [r["username"] for r in rows] == ["alice", "bob", "carol"]

    created = await pg_gateway.insert("users", {"username": "dave", "zip": "007"})
    assert created["id"] == 4 and created["zip"] == "007"

    same = await pg_gateway.update("users", "id", 4, {"username": "dave"})
    assert same is not None and same["username"] == "dave"
    assert await pg_gateway.update("users", "id", 999, {"username": "x"}) is None

    assert (await pg_gateway.select_by_id("users", "id", 4))["zip"] == "007"
    assert await pg_gateway.delete("users", "id", 4) is True
    assert await pg_gateway.delete("users", "id", 4) is False


async def test_filters_are_typed_not_guessed(pg_gateway: DatabaseGateway) -> None:
    # A text column compared with a text value: no int coercion, no DataError.
    rows, total = await pg_gateway.select("users", filters=[("zip", "eq", "00123")])
    assert total == 1 and rows[0]["username"] == "alice"
    rows, _ = await pg_gateway.select("users", filters=[("active", "eq", False)])
    assert [r["username"] for r in rows] == ["bob"]
    rows, _ = await pg_gateway.select("users", filters=[("zip", "is_null", True)])
    assert [r["username"] for r in rows] == ["carol"]
    rows, _ = await pg_gateway.select("users", filters=[("username", "like", "%a%")])
    assert {r["username"] for r in rows} == {"alice", "carol"}


async def test_named_parameters(pg_gateway: DatabaseGateway) -> None:
    rows = await pg_gateway.execute_query(
        "SELECT username FROM users WHERE zip = :zip OR id = :id::int OR id = :id",
        {"zip": "90210", "id": 1},
    )
    assert {r["username"] for r in rows} == {"alice", "bob"}
    rows = await pg_gateway.execute_query(
        "SELECT 'a:b' AS lit, ':zip' AS s WHERE 1 = :one", {"one": 1}
    )
    assert rows == [{"lit": "a:b", "s": ":zip"}]
    with pytest.raises(ValueError, match="Missing value"):
        await pg_gateway.execute_query("SELECT :missing", {})


async def test_row_estimates_after_analyze(pg_gateway: DatabaseGateway) -> None:
    await pg_gateway.execute_query("ANALYZE users")
    estimates = await pg_gateway.row_estimates(["users", "does_not_exist"])
    assert estimates["users"] == 3
    assert estimates["does_not_exist"] is None


async def test_stream_select_batches_and_limit(pg_gateway: DatabaseGateway) -> None:
    batches = [
        b
        async for b in pg_gateway.stream_select(
            "users", ["id", "username"], sort=[("id", "asc")], batch_size=2
        )
    ]
    assert [len(b) for b in batches] == [2, 1]
    assert [r["username"] for b in batches for r in b] == ["alice", "bob", "carol"]
    assert set(batches[0][0]) == {"id", "username"}
    limited = [
        b
        async for b in pg_gateway.stream_select(
            "users",
            filters=[("active", "eq", True)],
            sort=[("id", "desc")],
            batch_size=10,
            limit=1,
            statement_timeout_ms=5_000,
        )
    ]
    assert [r["username"] for b in limited for r in b] == ["carol"]
    # The pooled connection is back in a clean state (transaction closed).
    assert (await pg_gateway.execute_query("SELECT COUNT(*) AS n FROM users"))[0]["n"] == 3
