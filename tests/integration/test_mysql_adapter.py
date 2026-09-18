"""MySQL adapter against a real server."""

import pytest

from warp.application.ports.database import DatabaseGateway

pytestmark = pytest.mark.integration


async def test_introspection_uses_portable_column_keys(mysql_gateway: DatabaseGateway) -> None:
    assert "users" in await mysql_gateway.get_tables()  # TABLE_NAME key on MySQL 8
    schema = await mysql_gateway.get_table_schema("users")
    names = [c["name"] for c in schema["columns"]]
    assert names[:4] == ["id", "username", "zip", "active"]
    assert schema["primary_key"] == "id"


async def test_update_with_unchanged_values_returns_the_row(mysql_gateway: DatabaseGateway) -> None:
    # FOUND_ROWS: rowcount reports matched rows, so this is not mistaken for "not found".
    row = await mysql_gateway.update("users", "id", 1, {"username": "alice"})
    assert row is not None and row["username"] == "alice"
    assert await mysql_gateway.update("users", "id", 999, {"username": "x"}) is None


async def test_crud_round_trip(mysql_gateway: DatabaseGateway) -> None:
    created = await mysql_gateway.insert("users", {"username": "dave", "zip": "007"})
    assert created["username"] == "dave" and created["zip"] == "007"
    rows, total = await mysql_gateway.select("users", filters=[("zip", "eq", "00123")])
    assert total == 1 and rows[0]["username"] == "alice"
    assert await mysql_gateway.delete("users", "id", created["id"]) is True


async def test_named_parameters_and_percent(mysql_gateway: DatabaseGateway) -> None:
    rows = await mysql_gateway.execute_query(
        "SELECT username FROM users WHERE (zip = :zip OR id = :id) AND username LIKE 'a%' OR id = :id",
        {"zip": "90210", "id": 1},
    )
    assert {r["username"] for r in rows} == {"alice"}
    rows = await mysql_gateway.execute_query(
        "SELECT username FROM users WHERE username LIKE :pat", {"pat": "%o%"}
    )
    assert {r["username"] for r in rows} == {"bob", "carol"}


async def test_row_estimates(mysql_gateway: DatabaseGateway) -> None:
    estimates = await mysql_gateway.row_estimates(["users", "does_not_exist"])
    assert estimates["users"] is None or isinstance(estimates["users"], int)
    assert estimates["does_not_exist"] is None


async def test_stream_select_batches_and_limit(mysql_gateway: DatabaseGateway) -> None:
    batches = [
        b
        async for b in mysql_gateway.stream_select(
            "users", ["id", "username"], sort=[("id", "asc")], batch_size=2
        )
    ]
    assert [len(b) for b in batches] == [2, 1]
    assert [r["username"] for b in batches for r in b] == ["alice", "bob", "carol"]
    limited = [
        b
        async for b in mysql_gateway.stream_select(
            "users",
            filters=[("active", "eq", True)],
            sort=[("id", "desc")],
            batch_size=10,
            limit=1,
            statement_timeout_ms=5_000,
        )
    ]
    assert [r["username"] for b in limited for r in b] == ["carol"]
    # The unbuffered cursor was drained/closed; the connection is reusable.
    assert (await mysql_gateway.execute_query("SELECT COUNT(*) AS n FROM users"))[0]["n"] == 3
