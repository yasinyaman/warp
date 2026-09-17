"""The whole hexagon over a real PostgreSQL: lifespan, CRUD routes, validation, raw SQL."""

import pytest
from fastapi.testclient import TestClient

from warp.adapters.inbound.http.app import create_app, runtime_of
from warp.application.config import DatabaseConfig, RuntimeEnv, Settings
from warp.infrastructure.bootstrap import build_container

pytestmark = pytest.mark.integration


@pytest.fixture
def client(pg_gateway, postgres_config: DatabaseConfig, tmp_path):
    settings = Settings(
        databases=[postgres_config],
        settings={
            "catalog": {"storage_path": str(tmp_path / "catalogs")},
            "enable_raw_query": True,
            "raw_query_whitelist": ["SELECT"],
        },
    )
    app = create_app(container_factory=lambda: build_container(settings), env=RuntimeEnv())
    with TestClient(app) as c:
        yield c
    assert runtime_of(app).is_ready is False


def test_startup_discovers_schema(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "healthy"
    info = client.get("/info").json()
    assert "users" in info["databases"]["pg"]["tables"]


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
