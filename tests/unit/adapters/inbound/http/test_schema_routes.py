"""Schema endpoints over a mock gateway via TestClient."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import MockDatabaseAdapter
from warp.adapters.inbound.http.auth import AuthManager
from warp.adapters.inbound.http.routes.crud import RouterFactory
from warp.adapters.inbound.http.routes.schema import create_schema_router, primary_key_columns
from warp.application.config import ApiKeyConfig, AuthConfig
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.schema import ColumnSchema, DatabaseSchema, TableSchema

USERS = TableSchema(
    table_name="users",
    columns=[
        ColumnSchema(name="id", type="integer", nullable=False, udt_name="int4"),
        ColumnSchema(name="username", type="character varying", nullable=False, max_length=50),
        ColumnSchema(name="balance", type="numeric", nullable=True, precision=12, scale=2),
        ColumnSchema(name="created_at", type="timestamp without time zone", default="now()"),
    ],
    primary_key="id",
)
ORDERS = TableSchema(
    table_name="orders",
    columns=[
        ColumnSchema(name="user_id", type="integer", nullable=False),
        ColumnSchema(name="sku", type="varchar", nullable=False),
    ],
    primary_key=["user_id", "sku"],
)
SCHEMA = DatabaseSchema(database_name="testdb", tables={"users": USERS, "orders": ORDERS})


def _db() -> MockDatabaseAdapter:
    db = MockDatabaseAdapter()
    db.add_mock_table(
        "users",
        {"table_name": "users", "primary_key": "id"},
        [
            {"id": 1, "username": "alice", "balance": 10.5, "created_at": None},
            {"id": 2, "username": "bob", "balance": None, "created_at": None},
        ],
    )
    db.add_mock_table("orders", {"table_name": "orders"}, [])
    return db


def _app(auth_manager: AuthManager | None = None, with_crud: bool = True) -> FastAPI:
    db = _db()
    app = FastAPI()
    app.include_router(create_schema_router("testdb", SCHEMA, db, auth_manager), prefix="/api/v1")
    if with_crud:
        factory = RouterFactory(db=db, schema_analyzer=SchemaAnalyzer(db))
        app.include_router(factory.create_router(USERS), prefix="/api/v1")
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app())


class TestDatabaseSchema:
    def test_lists_typed_columns_and_estimates(self, client):
        r = client.get("/api/v1/schema")
        assert r.status_code == 200
        body = r.json()
        assert body["database"] == "testdb"
        users = body["tables"]["users"]
        assert users["row_estimate"] == 2
        assert users["primary_key"] == ["id"]
        cols = {c["name"]: c for c in users["columns"]}
        assert cols["id"]["kind"] == "int"
        assert cols["username"]["kind"] == "str"
        assert cols["username"]["max_length"] == 50
        assert cols["balance"] == {
            "name": "balance",
            "type": "numeric",
            "full_type": None,
            "udt_name": None,
            "kind": "float",
            "nullable": True,
            "precision": 12,
            "scale": 2,
            "max_length": None,
            "default": None,
        }
        assert cols["created_at"]["kind"] == "datetime"
        assert cols["created_at"]["default"] == "now()"

    def test_estimates_can_be_skipped(self, client):
        body = client.get("/api/v1/schema?estimates=false").json()
        assert body["tables"]["users"]["row_estimate"] is None

    def test_composite_primary_key_is_a_list(self, client):
        assert client.get("/api/v1/schema").json()["tables"]["orders"]["primary_key"] == [
            "user_id",
            "sku",
        ]


class TestTableSchema:
    def test_table_schema(self, client):
        r = client.get("/api/v1/users/schema")
        assert r.status_code == 200
        assert r.json()["table"] == "users"
        assert r.json()["row_estimate"] == 2
        assert [c["name"] for c in r.json()["columns"]] == [
            "id",
            "username",
            "balance",
            "created_at",
        ]

    def test_unknown_table_404(self, client):
        assert client.get("/api/v1/nope/schema").status_code == 404

    def test_schema_route_precedes_get_by_id(self, client):
        # Without the schema router mounted first, /users/schema would hit
        # GET /users/{id} and fail id parsing with 422.
        assert client.get("/api/v1/users/schema").status_code == 200
        assert client.get("/api/v1/users/1").status_code == 200
        assert client.get("/api/v1/users/abc").status_code == 422


class TestAuth:
    def test_requires_read_permission(self):
        manager = AuthManager(
            AuthConfig(
                enabled=True,
                api_keys=[
                    ApiKeyConfig(key="reader", permissions=["read"]),
                    ApiKeyConfig(key="writer", permissions=["create"]),
                ],
            )
        )
        c = TestClient(_app(manager, with_crud=False))
        assert c.get("/api/v1/schema").status_code == 401
        assert c.get("/api/v1/schema", headers={"X-API-Key": "writer"}).status_code == 403
        assert c.get("/api/v1/schema", headers={"X-API-Key": "reader"}).status_code == 200
        assert c.get("/api/v1/users/schema", headers={"X-API-Key": "reader"}).status_code == 200


def test_primary_key_columns_helper():
    assert primary_key_columns(USERS) == ["id"]
    assert primary_key_columns(ORDERS) == ["user_id", "sku"]
    assert primary_key_columns(TableSchema(table_name="t")) == []
