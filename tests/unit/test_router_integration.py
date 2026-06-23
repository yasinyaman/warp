"""End-to-end router tests: CRUD endpoints over a mock adapter via TestClient.

Exercises router_factory + crud + filtering/sorting/pagination together, and
verifies validation behavior (404s, invalid fields, mass-assignment 400s).
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from warp.api.router_factory import RouterFactory
from warp.schema.analyzer import SchemaAnalyzer
from warp.schema.models import ColumnSchema, TableSchema

SCHEMA = TableSchema(
    table_name="users",
    columns=[
        ColumnSchema(name="id", type="integer", nullable=False, extra="auto_increment"),
        ColumnSchema(name="username", type="varchar", nullable=False),
        ColumnSchema(name="email", type="varchar", nullable=False),
        ColumnSchema(name="status", type="varchar", nullable=True, default="active"),
        ColumnSchema(name="created_at", type="timestamp", nullable=True, default="now()"),
    ],
    primary_key="id",
)


@pytest.fixture
def client(mock_db):
    mock_db.add_mock_table(
        "users",
        {"table_name": "users", "primary_key": "id"},
        [
            {"id": 1, "username": "alice", "email": "alice@x.com", "status": "active"},
            {"id": 2, "username": "bob", "email": "bob@x.com", "status": "inactive"},
        ],
    )
    factory = RouterFactory(
        db=mock_db,
        schema_analyzer=SchemaAnalyzer(mock_db),
        readonly_columns=["created_at", "updated_at"],
    )
    app = FastAPI()
    app.include_router(factory.create_router(SCHEMA), prefix="/api/v1")
    return TestClient(app)


class TestRead:
    def test_list(self, client):
        r = client.get("/api/v1/users")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_list_pagination(self, client):
        r = client.get("/api/v1/users?limit=1&offset=0")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

    def test_list_with_sort_shortcut(self, client):
        assert client.get("/api/v1/users?sort=-id").status_code == 200

    def test_list_with_filter(self, client):
        assert client.get("/api/v1/users?filter[status]=active").status_code == 200

    def test_list_rejects_unknown_field(self, client):
        r = client.get("/api/v1/users?fields=id,does_not_exist")
        assert r.status_code == 400

    def test_get_by_id(self, client):
        r = client.get("/api/v1/users/1")
        assert r.status_code == 200
        assert r.json()["username"] == "alice"

    def test_get_missing_404(self, client):
        assert client.get("/api/v1/users/999").status_code == 404


class TestInjectionThroughApi:
    def test_malicious_filter_value_is_harmless(self, client):
        # The value is parameterized, so a SQL payload in the value is inert.
        r = client.get("/api/v1/users?filter[status]=1;DROP TABLE users;--")
        assert r.status_code == 200

    def test_limit_over_max_rejected(self, client):
        # Query bound le=max_limit (default 1000) -> 422 from FastAPI validation.
        assert client.get("/api/v1/users?limit=99999").status_code == 422


class TestWrite:
    def test_create(self, client):
        r = client.post("/api/v1/users", json={"username": "carol", "email": "c@x.com"})
        assert r.status_code == 201
        assert r.json()["username"] == "carol"

    def test_create_rejects_readonly_column(self, client):
        r = client.post(
            "/api/v1/users",
            json={"username": "x", "email": "x@x.com", "created_at": "2020-01-01T00:00:00"},
        )
        assert r.status_code == 400

    def test_update(self, client):
        r = client.put("/api/v1/users/1", json={"status": "archived"})
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

    def test_update_rejects_pk_change(self, client):
        r = client.put("/api/v1/users/1", json={"id": 999, "status": "x"})
        assert r.status_code == 400

    def test_update_missing_404(self, client):
        assert client.put("/api/v1/users/999", json={"status": "x"}).status_code == 404

    def test_delete(self, client):
        assert client.delete("/api/v1/users/1").status_code == 204
        assert client.get("/api/v1/users/1").status_code == 404

    def test_delete_missing_404(self, client):
        assert client.delete("/api/v1/users/999").status_code == 404
