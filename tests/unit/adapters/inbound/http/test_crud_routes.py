"""End-to-end router tests: CRUD endpoints over a mock adapter via TestClient.

Exercises router_factory + crud + filtering/sorting/pagination together, and
verifies validation behavior (404s, invalid fields, mass-assignment 400s).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from warp.adapters.inbound.http.routes.crud import RouterFactory
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.schema import ColumnSchema, TableSchema

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


@pytest.fixture
def big_limit_client(mock_db):
    mock_db.add_mock_table("users", {"table_name": "users", "primary_key": "id"}, [])
    factory = RouterFactory(db=mock_db, schema_analyzer=SchemaAnalyzer(mock_db), max_limit=5000)
    app = FastAPI()
    app.include_router(factory.create_router(SCHEMA), prefix="/api/v1")
    return TestClient(app)


class TestInputValidation:
    """Bad client input is a 4xx, never a 500 from an unhandled ValueError."""

    def test_non_numeric_id_is_422(self, client):
        for method in ("get", "put", "patch", "delete"):
            kwargs = {"json": {"username": "x"}} if method in ("put", "patch") else {}
            r = getattr(client, method)("/api/v1/users/abc", **kwargs)
            assert r.status_code == 422, method
            assert "expected an integer" in r.json()["detail"]

    def test_get_rejects_unknown_fields(self, client):
        r = client.get("/api/v1/users/1?fields=id,nope")
        assert r.status_code == 400
        assert "nope" in r.json()["detail"]
        assert client.get("/api/v1/users/1?fields=id,username").status_code == 200

    def test_filter_value_of_wrong_kind_is_400(self, client):
        r = client.get("/api/v1/users?filter[id]=abc")
        assert r.status_code == 400
        assert "must be an integer" in r.json()["detail"]

    def test_unknown_filter_column_is_400(self, client):
        assert client.get("/api/v1/users?filter[nope]=1").status_code == 400

    def test_bad_sort_is_400(self, client):
        assert client.get("/api/v1/users?sort=id:sideways").status_code == 400
        assert client.get("/api/v1/users?sort=nope:asc").status_code == 400

    def test_text_filter_values_are_not_coerced(self, client, mock_db):
        # "007" on a varchar column must reach the adapter as text.
        captured = {}

        original = mock_db.select

        async def spy(*args, **kwargs):
            captured.update(kwargs)
            return await original(*args, **kwargs)

        mock_db.select = spy
        assert client.get("/api/v1/users?filter[username]=007").status_code == 200
        assert captured["filters"] == [("username", "eq", "007")]

    def test_configured_max_limit_above_1000(self, big_limit_client):
        assert big_limit_client.get("/api/v1/users?limit=2000").status_code == 200
        assert big_limit_client.get("/api/v1/users?limit=6000").status_code == 422
