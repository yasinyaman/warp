"""Oracle adapter against a real server (Oracle ODBC driver via aioodbc).

Oracle is the engine competing semantic layers support "through sqlglot" with
no live tests, so these run against a real database or not at all.
"""

import pytest

from warp.adapters.outbound.db.comment_reader import CommentReader
from warp.adapters.outbound.db.sample_reader import SampleReader
from warp.application.ports.database import DatabaseGateway

pytestmark = pytest.mark.integration


async def test_introspection(oracle_gateway: DatabaseGateway) -> None:
    # Oracle stores unquoted identifiers upper-cased.
    assert "USERS" in await oracle_gateway.get_tables()
    schema = await oracle_gateway.get_table_schema("users")
    cols = {c["name"]: c for c in schema["columns"]}
    assert list(cols)[:4] == ["ID", "USERNAME", "ZIP", "ACTIVE"]
    assert schema["primary_key"] == "ID"
    assert cols["ID"]["extra"] == "identity" and cols["ID"]["key"] == "PRI"
    assert cols["USERNAME"]["full_type"] == "varchar2(50)"
    assert cols["USERNAME"]["nullable"] is False and cols["ZIP"]["nullable"] is True
    assert cols["ACTIVE"]["type"] == "number"
    assert cols["CREATED_AT"]["type"].startswith("timestamp")


async def test_the_session_user_is_the_schema(oracle_gateway: DatabaseGateway) -> None:
    # `database` is a service name, so the "database name is the schema"
    # fallback is wrong for Oracle; the adapter asks the session instead.
    assert oracle_gateway._schema == "WARP"


async def test_crud_round_trip_refetches(oracle_gateway: DatabaseGateway) -> None:
    rows, total = await oracle_gateway.select("users", sort=[("id", "asc")])
    assert total == 3 and [r["USERNAME"] for r in rows] == ["alice", "bob", "carol"]

    # Oracle has no RETURNING the gateway can consume, so writes re-select.
    created = await oracle_gateway.insert("users", {"username": "dave", "zip": "007"})
    assert created["USERNAME"] == "dave" and created["ZIP"] == "007"

    same = await oracle_gateway.update("users", {"id": created["ID"]}, {"username": "dave2"})
    assert same is not None and same["USERNAME"] == "dave2"
    assert await oracle_gateway.update("users", {"id": 999}, {"username": "x"}) is None

    assert (await oracle_gateway.select_by_id("users", {"id": created["ID"]}))["ZIP"] == "007"
    assert await oracle_gateway.delete("users", {"id": created["ID"]}) is True
    assert await oracle_gateway.delete("users", {"id": created["ID"]}) is False


async def test_typed_filters_and_pagination(oracle_gateway: DatabaseGateway) -> None:
    rows, total = await oracle_gateway.select("users", filters=[("zip", "eq", "00123")])
    assert total == 1 and rows[0]["USERNAME"] == "alice"
    rows, _ = await oracle_gateway.select("users", filters=[("zip", "is_null", True)])
    assert [r["USERNAME"] for r in rows] == ["carol"]
    rows, _ = await oracle_gateway.select("users", filters=[("username", "like", "%a%")])
    assert {r["USERNAME"] for r in rows} == {"alice", "carol"}

    # Oracle's row-limiting clause needs no ORDER BY, and rejects SQL Server's
    # `ORDER BY (SELECT NULL)` filler.
    page, total = await oracle_gateway.select("users", pagination={"limit": 2, "offset": 1})
    assert total == 3 and len(page) == 2
    page, _ = await oracle_gateway.select(
        "users", pagination={"limit": 2, "offset": 2}, sort=[("id", "desc")]
    )
    assert [r["USERNAME"] for r in page] == ["alice"]


async def test_named_parameters_and_literals(oracle_gateway: DatabaseGateway) -> None:
    rows = await oracle_gateway.execute_query(
        "SELECT username FROM users WHERE zip = :zip OR id = :id OR id = :id",
        {"zip": "90210", "id": 1},
    )
    assert {r["USERNAME"] for r in rows} == {"alice", "bob"}
    rows = await oracle_gateway.execute_query(
        "SELECT 'a:b' AS lit, ':zip' AS s, '100%' AS pct FROM DUAL WHERE 1 = :one", {"one": 1}
    )
    assert rows == [{"LIT": "a:b", "S": ":zip", "PCT": "100%"}]
    with pytest.raises(ValueError, match="Missing value"):
        await oracle_gateway.execute_query("SELECT :missing FROM DUAL", {})


async def test_foreign_keys_and_indexes(oracle_gateway: DatabaseGateway) -> None:
    await oracle_gateway.execute_query(
        "BEGIN EXECUTE IMMEDIATE 'DROP TABLE orders'; "
        "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
    )
    await oracle_gateway.execute_query(
        "CREATE TABLE orders (id NUMBER PRIMARY KEY, user_id NUMBER NOT NULL, "
        "total NUMBER(10,2), CONSTRAINT fk_orders_user FOREIGN KEY (user_id) "
        "REFERENCES users (id))"
    )
    await oracle_gateway.execute_query("CREATE UNIQUE INDEX ix_orders_total ON orders (total)")

    schema = await oracle_gateway.get_table_schema("orders")
    assert schema["foreign_keys"] == [
        {
            "column": "USER_ID",
            "references_table": "USERS",
            "references_column": "ID",
            "constraint_name": "FK_ORDERS_USER",
        }
    ]
    # The primary key's own index is excluded, as it is for the other engines.
    assert schema["indexes"] == [{"name": "IX_ORDERS_TOTAL", "columns": ["TOTAL"], "unique": True}]
    total = {c["name"]: c for c in schema["columns"]}["TOTAL"]
    assert total["full_type"] == "number(10,2)"
    await oracle_gateway.execute_query("DROP TABLE orders")


async def test_catalog_intelligence(oracle_gateway: DatabaseGateway) -> None:
    await oracle_gateway.execute_query("COMMENT ON TABLE users IS 'Registered users'")
    await oracle_gateway.execute_query("COMMENT ON COLUMN users.zip IS 'Postal code'")

    comments = CommentReader(oracle_gateway, db_type="oracle", schema="WARP")
    per_table = await comments.read_table_comments("USERS")
    assert per_table.table_comment == "Registered users"
    assert per_table.column_comments == {"ZIP": "Postal code"}
    everything = await comments.read_all_comments()
    assert everything["USERS"].table_comment == "Registered users"

    samples = SampleReader(oracle_gateway, db_type="oracle", schema="WARP")
    table_samples = await samples.read_table_samples("users", sample_limit=2)
    assert len(table_samples.column_samples["USERNAME"]) == 2
    assert table_samples.column_stats["ZIP"].null_count == 1
    # Stats cover the whole table, not just the sampled rows.
    assert table_samples.column_stats["USERNAME"].distinct_count == 3


async def test_row_estimates_need_statistics(oracle_gateway: DatabaseGateway) -> None:
    # NUM_ROWS is NULL until the table is analysed, which is why this is an
    # estimate and never a COUNT(*).
    await oracle_gateway.execute_query("BEGIN DBMS_STATS.GATHER_TABLE_STATS(USER, 'USERS'); END;")
    estimates = await oracle_gateway.row_estimates(["users", "missing"])
    assert estimates["users"] == 3
    assert estimates["missing"] is None


async def test_an_undeclared_number_cannot_round_trip(oracle_gateway: DatabaseGateway) -> None:
    """The one Oracle fidelity limit Warp cannot fix, and the DDL that does.

    The ODBC driver hands an undeclared NUMBER over as an IEEE double, so the
    value is already rounded before Python sees it — there is no later point
    at which it could be recovered. Declaring the precision makes the driver
    return an exact Decimal instead.
    """
    big = 9007199254740993  # 2**53 + 1: the first integer a double cannot hold
    for name, ddl in (
        ("bare_number", "id NUMBER PRIMARY KEY"),
        ("exact_number", "id NUMBER(38) PRIMARY KEY"),
    ):
        await oracle_gateway.execute_query(
            f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {name}'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
        )
        await oracle_gateway.execute_query(f"CREATE TABLE {name} ({ddl})")
        await oracle_gateway.execute_query(f"INSERT INTO {name} VALUES ({big})")

    bare = await oracle_gateway.get_table_schema("bare_number")
    exact = await oracle_gateway.get_table_schema("exact_number")
    assert bare["columns"][0]["inexact"] is True
    assert exact["columns"][0]["inexact"] is False

    bare_rows, _ = await oracle_gateway.select("bare_number")
    exact_rows, _ = await oracle_gateway.select("exact_number")
    # Flagged, and for good reason: the value really is gone.
    assert bare_rows[0]["ID"] != big
    assert exact_rows[0]["ID"] == big

    for name in ("bare_number", "exact_number"):
        await oracle_gateway.execute_query(f"DROP TABLE {name}")
