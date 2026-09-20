"""SQL Server adapter against a real server (ODBC Driver 18 via aioodbc)."""

import uuid
from datetime import timedelta

import pytest

from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.application.ports.database import DatabaseGateway

pytestmark = pytest.mark.integration


async def test_introspection(mssql_gateway: DatabaseGateway) -> None:
    assert "users" in await mssql_gateway.get_tables()
    schema = await mssql_gateway.get_table_schema("users")
    cols = {c["name"]: c for c in schema["columns"]}
    assert list(cols)[:4] == ["id", "username", "zip", "active"]
    assert schema["primary_key"] == "id"
    assert cols["id"]["extra"] == "identity" and cols["id"]["key"] == "PRI"
    assert cols["username"]["full_type"] == "nvarchar(50)"
    assert cols["username"]["nullable"] is False and cols["zip"]["nullable"] is True
    assert cols["active"]["type"] == "bit" and cols["active"]["default"] == "1"
    assert cols["created_at"]["type"] == "datetime2"


async def test_crud_round_trip_with_output(mssql_gateway: DatabaseGateway) -> None:
    rows, total = await mssql_gateway.select("users", sort=[("id", "asc")])
    assert total == 3 and [r["username"] for r in rows] == ["alice", "bob", "carol"]

    created = await mssql_gateway.insert("users", {"username": "dave", "zip": "007"})
    assert created["id"] == 4 and created["zip"] == "007" and created["active"] is True

    same = await mssql_gateway.update("users", {"id": 4}, {"username": "dave"})
    assert same is not None and same["username"] == "dave"
    assert await mssql_gateway.update("users", {"id": 999}, {"username": "x"}) is None

    assert (await mssql_gateway.select_by_id("users", {"id": 4}))["zip"] == "007"
    assert await mssql_gateway.delete("users", {"id": 4}) is True
    assert await mssql_gateway.delete("users", {"id": 4}) is False


async def test_typed_filters_and_pagination(mssql_gateway: DatabaseGateway) -> None:
    rows, total = await mssql_gateway.select("users", filters=[("zip", "eq", "00123")])
    assert total == 1 and rows[0]["username"] == "alice"
    rows, _ = await mssql_gateway.select("users", filters=[("active", "eq", False)])
    assert [r["username"] for r in rows] == ["bob"]
    rows, _ = await mssql_gateway.select("users", filters=[("zip", "is_null", True)])
    assert [r["username"] for r in rows] == ["carol"]
    rows, _ = await mssql_gateway.select("users", filters=[("username", "like", "%a%")])
    assert {r["username"] for r in rows} == {"alice", "carol"}

    # OFFSET/FETCH needs an ORDER BY; the builder injects a stable dummy one.
    page, total = await mssql_gateway.select("users", pagination={"limit": 2, "offset": 1})
    assert total == 3 and len(page) == 2
    page, _ = await mssql_gateway.select(
        "users", pagination={"limit": 2, "offset": 2}, sort=[("id", "desc")]
    )
    assert [r["username"] for r in page] == ["alice"]


async def test_named_parameters_and_literals(mssql_gateway: DatabaseGateway) -> None:
    rows = await mssql_gateway.execute_query(
        "SELECT username FROM users WHERE zip = :zip OR id = :id OR id = :id",
        {"zip": "90210", "id": 1},
    )
    assert {r["username"] for r in rows} == {"alice", "bob"}
    rows = await mssql_gateway.execute_query(
        "SELECT 'a:b' AS lit, ':zip' AS s, '100%' AS pct WHERE 1 = :one", {"one": 1}
    )
    assert rows == [{"lit": "a:b", "s": ":zip", "pct": "100%"}]
    await mssql_gateway.execute_query("DROP TABLE IF EXISTS scratch")
    assert await mssql_gateway.execute_query("CREATE TABLE scratch (id INT)") == []
    await mssql_gateway.execute_query("DROP TABLE scratch")
    with pytest.raises(ValueError, match="Missing value"):
        await mssql_gateway.execute_query("SELECT :missing", {})


async def test_catalog_intelligence(mssql_gateway: DatabaseGateway) -> None:
    await mssql_gateway.execute_query(
        "EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'Registered users', "
        "@level0type = N'SCHEMA', @level0name = N'dbo', "
        "@level1type = N'TABLE', @level1name = N'users'"
    )
    await mssql_gateway.execute_query(
        "EXEC sp_addextendedproperty @name = N'MS_Description', @value = N'Postal code', "
        "@level0type = N'SCHEMA', @level0name = N'dbo', "
        "@level1type = N'TABLE', @level1name = N'users', "
        "@level2type = N'COLUMN', @level2name = N'zip'"
    )

    comments = CommentReader(mssql_gateway, db_type="mssql", schema="dbo")
    per_table = await comments.read_table_comments("users")
    assert per_table.table_comment == "Registered users"
    assert per_table.column_comments == {"zip": "Postal code"}
    everything = await comments.read_all_comments()
    assert everything["users"].table_comment == "Registered users"
    assert everything["users"].column_comments == {"zip": "Postal code"}

    samples = SampleReader(mssql_gateway, db_type="mssql", schema="dbo")
    table_samples = await samples.read_table_samples("users", sample_limit=2)
    assert table_samples.row_count == 3
    assert len(table_samples.column_samples["username"]) == 2
    assert table_samples.column_stats["zip"].null_count == 1
    # stats cover the whole table, not just the sampled rows
    assert table_samples.column_stats["username"].distinct_count == 3


async def test_writes_fall_back_when_the_table_has_triggers(
    mssql_gateway: DatabaseGateway,
) -> None:
    await mssql_gateway.execute_query(
        "CREATE TRIGGER trg_users_audit ON users AFTER INSERT, UPDATE, DELETE "
        "AS BEGIN SET NOCOUNT ON; END"
    )
    created = await mssql_gateway.insert("users", {"username": "erin", "zip": "111"})
    assert created["username"] == "erin" and created["id"] == 4
    updated = await mssql_gateway.update("users", {"id": created["id"]}, {"zip": "222"})
    assert updated is not None and updated["zip"] == "222"
    assert await mssql_gateway.update("users", {"id": 999}, {"zip": "x"}) is None
    assert await mssql_gateway.delete("users", {"id": created["id"]}) is True
    assert await mssql_gateway.delete("users", {"id": created["id"]}) is False
    rows, total = await mssql_gateway.select("users")
    assert total == 3


async def test_datetimeoffset_and_uniqueidentifier(mssql_gateway: DatabaseGateway) -> None:
    await mssql_gateway.execute_query("DROP TABLE IF EXISTS events")
    await mssql_gateway.execute_query(
        "CREATE TABLE events (id UNIQUEIDENTIFIER PRIMARY KEY DEFAULT NEWID(), "
        "at DATETIMEOFFSET NOT NULL)"
    )
    await mssql_gateway.execute_query(
        "INSERT INTO events (at) VALUES ('2024-03-09 14:30:15.1234567 +03:00')"
    )
    rows = await mssql_gateway.execute_query("SELECT id, at FROM events")
    assert rows[0]["at"].utcoffset() == timedelta(hours=3)
    assert (rows[0]["at"].year, rows[0]["at"].microsecond) == (2024, 123456)
    assert uuid.UUID(str(rows[0]["id"]))
    schema = await mssql_gateway.get_table_schema("events")
    assert {c["name"]: c["type"] for c in schema["columns"]} == {
        "id": "uniqueidentifier",
        "at": "datetimeoffset",
    }
    await mssql_gateway.execute_query("DROP TABLE events")
