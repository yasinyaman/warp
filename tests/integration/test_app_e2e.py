"""The whole hexagon over real databases: lifespan, CRUD routes, validation, raw SQL.

Runs once over PostgreSQL and once over SQL Server (skipped where the SQL
Server prerequisites are missing, see conftest).
"""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from warp.adapters.inbound.http.app import create_app, runtime_of
from warp.application.config import RuntimeEnv, Settings
from warp.infrastructure.bootstrap import build_container

pytestmark = pytest.mark.integration

BACKENDS = {"pg": ("pg_gateway", "postgres_config"), "mssql": ("mssql_gateway", "mssql_config")}


def _make_app(config, tmp_path):
    settings = Settings(
        databases=[config],
        settings={
            "catalog": {"storage_path": str(tmp_path / "catalogs")},
            "enable_raw_query": True,
            "raw_query_whitelist": ["SELECT"],
        },
    )
    return create_app(container_factory=lambda: build_container(settings), env=RuntimeEnv())


@pytest.fixture(params=list(BACKENDS))
def client(request: pytest.FixtureRequest, tmp_path):
    gateway_fixture, config_fixture = BACKENDS[request.param]
    request.getfixturevalue(gateway_fixture)  # fresh `users` table
    app = _make_app(request.getfixturevalue(config_fixture), tmp_path)
    with TestClient(app) as c:
        yield c
    assert runtime_of(app).is_ready is False


@pytest.fixture
def pg_client(pg_gateway, postgres_config, tmp_path):
    """PostgreSQL only, for assertions about PostgreSQL's own types."""
    app = _make_app(postgres_config, tmp_path)
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def ledger(pg_gateway):
    """A table with the types JSON cannot carry faithfully (decimal, timestamptz, bytea)."""
    await pg_gateway.execute_query("DROP TABLE IF EXISTS ledger")
    await pg_gateway.execute_query(
        "CREATE TABLE ledger (id BIGSERIAL PRIMARY KEY, amount NUMERIC(12,2) NOT NULL, "
        "booked_at TIMESTAMPTZ NOT NULL, note TEXT, raw BYTEA)"
    )
    await pg_gateway.execute_query(
        "INSERT INTO ledger (amount, booked_at, note, raw) VALUES "
        "(10.25, '2024-01-01T10:00:00+02:00', 'a', '\\x0102'), "
        "(-3.00, '2024-02-01T00:00:00Z', NULL, NULL)"
    )
    yield
    await pg_gateway.execute_query("DROP TABLE IF EXISTS ledger")


@pytest.fixture
def ledger_client(ledger, postgres_config, tmp_path):
    app = _make_app(postgres_config, tmp_path)
    with TestClient(app) as c:
        yield c


def test_startup_discovers_schema(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "healthy"
    info = client.get("/info").json()
    assert any("users" in db["tables"] for db in info["databases"].values())


def test_crud_over_http(client: TestClient) -> None:
    listing = client.get("/api/v1/users?sort=id:asc").json()
    assert listing["total"] == 3
    assert client.get("/api/v1/users/1").json()["username"] == "alice"

    created = client.post("/api/v1/users", json={"username": "dave", "zip": "007"})
    assert created.status_code == 201 and created.json()["zip"] == "007"
    new_id = created.json()["id"]

    unchanged = client.put(f"/api/v1/users/{new_id}", json={"username": "dave"})
    assert unchanged.status_code == 200
    assert client.delete(f"/api/v1/users/{new_id}").status_code == 204
    assert client.get(f"/api/v1/users/{new_id}").status_code == 404


def test_validation_and_typed_filters(client: TestClient) -> None:
    assert client.get("/api/v1/users?filter[zip]=00123").json()["total"] == 1  # stays text
    assert client.get("/api/v1/users?filter[active]=false").json()["items"][0]["username"] == "bob"
    assert client.get("/api/v1/users?filter[id]=abc").status_code == 400
    assert client.get("/api/v1/users/abc").status_code == 422
    assert client.get("/api/v1/users/1?fields=nope").status_code == 400
    assert client.get("/api/v1/users?sort=id:sideways").status_code == 400
    r = client.post("/api/v1/users", json={"id": 99, "username": "x"})
    assert r.status_code == 422  # the auto-generated PK is not an accepted field
    r = client.put("/api/v1/users/1", json={"id": 99})
    assert r.status_code == 400  # PK is immutable on update
    assert client.get("/api/v1/users/1").json()["username"] == "alice"


def test_raw_query_with_named_params(client: TestClient) -> None:
    ok = client.post(
        "/api/v1/query/execute",
        json={"query": "SELECT username FROM users WHERE zip = :zip", "params": {"zip": "90210"}},
    )
    assert ok.status_code == 200 and ok.json()["rows"] == [{"username": "bob"}]
    missing = client.post("/api/v1/query/execute", json={"query": "SELECT :nope"})
    assert missing.status_code == 400
    stacked = client.post("/api/v1/query/execute", json={"query": "SELECT 1; DROP TABLE users"})
    assert stacked.status_code == 400
    assert client.get("/api/v1/users").json()["total"] == 3


def test_catalog_name_traversal_is_rejected(client: TestClient) -> None:
    assert client.delete("/api/v1/catalog/%2e%2e").status_code == 422


def test_schema_endpoint_over_postgres(pg_client: TestClient) -> None:
    table = pg_client.get("/api/v1/users/schema").json()
    assert table["primary_key"] == ["id"]
    cols = {c["name"]: c for c in table["columns"]}
    assert cols["id"]["kind"] == "int" and cols["id"]["nullable"] is False
    assert cols["username"]["kind"] == "str" and cols["username"]["max_length"] == 50
    assert cols["active"]["kind"] == "bool"
    assert cols["created_at"]["kind"] == "datetime"
    assert table["row_estimate"] is None or isinstance(table["row_estimate"], int)
    whole = pg_client.get("/api/v1/schema").json()
    assert whole["database"] == "pg" and "users" in whole["tables"]
    assert pg_client.get("/api/v1/nope/schema").status_code == 404


def test_db_scoped_and_alias_routes(client: TestClient) -> None:
    info = client.get("/info").json()
    database = next(iter(info["databases"]))
    assert client.get(f"/api/v1/{database}/users?sort=id:asc").json()["total"] == 3
    assert client.get(f"/api/v1/{database}/users/1").json()["username"] == "alice"
    assert client.get(f"/api/v1/{database}/users/schema").status_code == 200
    # The database-level endpoints must resolve under the scoped prefix too.
    assert client.get(f"/api/v1/{database}/schema").json()["database"] == database
    assert client.get(f"/api/v1/{database}/users/export?limit=1").status_code == 200
    # ...and the bare alias still answers for a single database.
    assert client.get("/api/v1/users?limit=1").status_code == 200
    caps = info["capabilities"]
    assert caps["db_prefix"] == "always" and caps["schema"] is True


def test_export_ndjson_over_postgres(pg_client: TestClient) -> None:
    r = pg_client.get("/api/v1/users/export?format=ndjson&fields=id,username&sort=id:asc")
    assert r.status_code == 200 and r.headers["x-export-format"] == "ndjson"
    rows = [json.loads(line) for line in r.text.splitlines()]
    assert rows == [
        {"id": 1, "username": "alice"},
        {"id": 2, "username": "bob"},
        {"id": 3, "username": "carol"},
    ]
    body = pg_client.post(
        "/api/v1/users/export",
        json={
            "filters": [{"column": "id", "op": "in", "value": [1, 3]}],
            "sort": [{"column": "id", "direction": "desc"}],
        },
    ).json()
    assert [i["username"] for i in body["items"]] == ["carol", "alice"]
    assert body["row_count"] == 2
    assert pg_client.get("/api/v1/users/export?filter[active][eq]=false").json()["row_count"] == 1
    assert "export" in pg_client.get("/info").json()["capabilities"]


def test_export_arrow_over_postgres(ledger_client: TestClient) -> None:
    r = ledger_client.get("/api/v1/ledger/export?format=arrow&sort=id:asc")
    assert r.status_code == 200
    table = pa.ipc.open_stream(r.content).read_all()
    assert table.num_rows == 2
    assert table.schema.field("id").type == pa.int64()
    assert table.schema.field("amount").type == pa.decimal128(12, 2)
    assert table.schema.field("booked_at").type == pa.timestamp("us", tz="UTC")
    assert table.schema.field("note").type == pa.string()
    assert table.schema.field("raw").type == pa.binary()
    assert table.column("amount").to_pylist() == [Decimal("10.25"), Decimal("-3.00")]
    assert table.column("booked_at").to_pylist() == [
        datetime(2024, 1, 1, 8, tzinfo=UTC),
        datetime(2024, 2, 1, tzinfo=UTC),
    ]
    assert table.column("note").to_pylist() == ["a", None]
    assert table.column("raw").to_pylist() == [b"\x01\x02", None]
    cols = {c["name"]: c for c in ledger_client.get("/api/v1/ledger/schema").json()["columns"]}
    assert (cols["amount"]["precision"], cols["amount"]["scale"]) == (12, 2)
    # JSON export of the same rows keeps decimals as text and bytes as base64.
    items = ledger_client.get("/api/v1/ledger/export?sort=id:asc").json()["items"]
    assert items[0]["amount"] == "10.25" and items[0]["raw"] == "AQI="


@pytest.fixture
async def order_lines(pg_gateway):
    """A composite primary key, which on an ERP schema most line tables have."""
    await pg_gateway.execute_query("DROP TABLE IF EXISTS siparis_satirlari")
    await pg_gateway.execute_query(
        "CREATE TABLE siparis_satirlari ("
        "siparis_id BIGINT NOT NULL, satir_no INT NOT NULL, urun TEXT NOT NULL, "
        "adet INT NOT NULL, PRIMARY KEY (siparis_id, satir_no))"
    )
    await pg_gateway.execute_query(
        "INSERT INTO siparis_satirlari (siparis_id, satir_no, urun, adet) VALUES "
        "(5, 1, 'a', 1), (5, 2, 'b', 2), (5, 3, 'c', 3), (6, 1, 'd', 4)"
    )
    yield
    await pg_gateway.execute_query("DROP TABLE IF EXISTS siparis_satirlari")


@pytest.fixture
def lines_client(order_lines, postgres_config, tmp_path):
    app = _make_app(postgres_config, tmp_path)
    with TestClient(app) as c:
        yield c


def test_the_whole_key_reaches_postgres(lines_client: TestClient) -> None:
    """The regression, against a real engine rather than a fake gateway.

    Before the fix the key collapsed to its first column, so this DELETE was
    `WHERE siparis_id = 5` with no LIMIT: all three lines of order 5, answered
    204. The unit test pins the SQL; this one pins what the database did with
    it, which is the half that actually lost the rows.
    """
    assert lines_client.get("/api/v1/siparis_satirlari/5/2").json()["urun"] == "b"

    assert lines_client.delete("/api/v1/siparis_satirlari/5/2").status_code == 204

    remaining = lines_client.get("/api/v1/siparis_satirlari?sort=satir_no:asc").json()["items"]
    left = sorted(r["satir_no"] for r in remaining if r["siparis_id"] == 5)
    assert left == [1, 3], "the rest of the order must still be there"
    assert any(r["siparis_id"] == 6 for r in remaining), "and so must the other order"


def test_an_update_addresses_one_line(lines_client: TestClient) -> None:
    r = lines_client.put("/api/v1/siparis_satirlari/5/3", json={"adet": 99})
    assert r.status_code == 200 and r.json()["adet"] == 99

    rows = lines_client.get("/api/v1/siparis_satirlari").json()["items"]
    assert sorted(r["adet"] for r in rows if r["siparis_id"] == 5) == [1, 2, 99]


def test_the_schema_endpoint_reports_both_key_columns(lines_client: TestClient) -> None:
    table = lines_client.get("/api/v1/siparis_satirlari/schema").json()
    assert table["primary_key"] == ["siparis_id", "satir_no"]


def test_the_path_is_named_after_the_key_columns(lines_client: TestClient) -> None:
    """What a spec reader — and a generated MCP tool — sees for the arity."""
    paths = lines_client.get("/openapi.json").json()["paths"]
    assert "/api/v1/siparis_satirlari/{siparis_id}/{satir_no}" in paths
    assert "/api/v1/siparis_satirlari/{id}" not in paths
