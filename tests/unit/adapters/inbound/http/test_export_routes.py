"""``GET|POST /{table}/export`` over the mock gateway (json, ndjson, arrow)."""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal

import pyarrow as pa
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import MockDatabaseAdapter
from warp.adapters.inbound.http.auth import AuthManager
from warp.adapters.inbound.http.governance import Governance
from warp.adapters.inbound.http.routes import export as export_module
from warp.adapters.inbound.http.routes.crud import RouterFactory
from warp.adapters.inbound.http.routes.export import create_export_router, effective_limit
from warp.application.config import ApiKeyConfig, AuthConfig, ExportConfig
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.masking import CatalogMasking, MaskingPolicy
from warp.domain.schema import ColumnSchema, TableSchema

PRODUCTS = TableSchema(
    table_name="products",
    columns=[
        ColumnSchema(name="id", type="integer", nullable=False, udt_name="int4"),
        ColumnSchema(name="name", type="character varying", nullable=False, max_length=50),
        ColumnSchema(name="price", type="numeric", precision=10, scale=2),
        ColumnSchema(name="active", type="boolean", nullable=False),
        ColumnSchema(name="created_at", type="timestamp with time zone", udt_name="timestamptz"),
        ColumnSchema(name="payload", type="bytea"),
    ],
    primary_key="id",
)


def _row(i: int) -> dict:
    return {
        "id": i,
        "name": f"product {i}",
        "price": Decimal(f"{i * 10}.50"),
        "active": i % 2 == 1,
        "created_at": datetime(2024, 1, i, tzinfo=UTC),
        "payload": bytes([i]) if i != 3 else None,
    }


def _db() -> MockDatabaseAdapter:
    db = MockDatabaseAdapter()
    db.add_mock_table(
        "products", {"table_name": "products", "primary_key": "id"}, [_row(i) for i in range(1, 6)]
    )
    return db


def _app(
    export: ExportConfig | None = None,
    auth_manager: AuthManager | None = None,
    with_crud: bool = True,
    db: MockDatabaseAdapter | None = None,
    masking: CatalogMasking | None = None,
) -> FastAPI:
    db = db or _db()
    app = FastAPI()
    router = create_export_router(
        PRODUCTS,
        db,
        export or ExportConfig(),
        auth_manager,
        None,
        Governance(masking=masking) if masking else None,
    )
    app.include_router(router, prefix="/api/v1")
    if with_crud:
        factory = RouterFactory(db=db, schema_analyzer=SchemaAnalyzer(db))
        app.include_router(factory.create_router(PRODUCTS), prefix="/api/v1")
    return app


@pytest.fixture
def db() -> MockDatabaseAdapter:
    return _db()


@pytest.fixture
def client(db: MockDatabaseAdapter) -> TestClient:
    return TestClient(_app(db=db))


class TestJson:
    def test_streams_full_document(self, client, db):
        r = client.get("/api/v1/products/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert r.headers["x-export-format"] == "json"
        assert r.headers["cache-control"] == "no-store"
        assert "x-export-max-rows" not in r.headers
        body = r.json()
        assert body["row_count"] == 5
        assert [item["id"] for item in body["items"]] == [1, 2, 3, 4, 5]
        first = body["items"][0]
        assert first["price"] == "10.50"  # Decimal → str, never float
        assert first["created_at"] == "2024-01-01T00:00:00+00:00"
        assert first["payload"] == base64.b64encode(b"\x01").decode()
        assert body["items"][2]["payload"] is None
        assert db.stream_calls[-1]["batch_size"] == 5000

    def test_rows_span_batches_without_broken_commas(self):
        db = _db()
        client = TestClient(_app(ExportConfig(batch_size=2), db=db))
        body = client.get("/api/v1/products/export").json()
        assert body["row_count"] == 5 and len(body["items"]) == 5
        assert db.stream_calls[-1]["batch_size"] == 2

    def test_empty_result_is_valid_json(self, client):
        body = client.get("/api/v1/products/export?filter[id][gt]=100").json()
        assert body == {"items": [], "row_count": 0}

    def test_fields_projection(self, client, db):
        body = client.get("/api/v1/products/export?fields=id,name").json()
        assert body["items"][0] == {"id": 1, "name": "product 1"}
        assert db.stream_calls[-1]["columns"] == ["id", "name"]

    def test_typed_get_filters_and_sort(self, client, db):
        r = client.get(
            "/api/v1/products/export",
            params={
                "filter[price][gt]": "20",
                "filter[active][eq]": "true",
                "sort": "id:desc",
                "limit": 1,
            },
        )
        assert r.status_code == 200
        assert [i["id"] for i in r.json()["items"]] == [5]
        call = db.stream_calls[-1]
        assert ("price", "gt", 20.0) in call["filters"]
        assert ("active", "eq", True) in call["filters"]
        assert call["sort"] == [("id", "desc")]
        assert call["limit"] == 1

    def test_timestamp_filter_is_typed(self, client, db):
        r = client.get(
            "/api/v1/products/export",
            params={"filter[created_at][gte]": "2024-01-04T00:00:00Z"},
        )
        assert [i["id"] for i in r.json()["items"]] == [4, 5]
        column, op, value = db.stream_calls[-1]["filters"][0]
        assert (column, op) == ("created_at", "gte") and isinstance(value, datetime)


class TestNdjson:
    def test_one_object_per_line(self, client):
        r = client.get("/api/v1/products/export?format=ndjson&fields=id,price")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert r.headers["x-export-format"] == "ndjson"
        lines = r.text.splitlines()
        assert len(lines) == 5
        assert json.loads(lines[0]) == {"id": 1, "price": "10.50"}
        assert r.text.endswith("\n")

    def test_empty_ndjson(self, client):
        r = client.get("/api/v1/products/export?format=ndjson&filter[id][gt]=100")
        assert r.status_code == 200 and r.text == ""


class TestPost:
    def test_long_in_list_in_body(self, client, db):
        r = client.post(
            "/api/v1/products/export",
            json={
                "fields": ["id"],
                "filters": [{"column": "id", "op": "in", "value": list(range(1, 1001))}],
                "sort": [{"column": "id", "direction": "desc"}],
                "limit": 3,
                "format": "ndjson",
            },
        )
        assert r.status_code == 200
        assert [json.loads(line)["id"] for line in r.text.splitlines()] == [5, 4, 3]
        column, op, value = db.stream_calls[-1]["filters"][0]
        assert (column, op) == ("id", "in") and len(value) == 1000 and value[0] == 1

    def test_body_values_are_coerced_by_kind(self, client, db):
        r = client.post(
            "/api/v1/products/export",
            json={"filters": [{"column": "price", "op": "gte", "value": "40"}]},
        )
        assert [i["id"] for i in r.json()["items"]] == [4, 5]
        assert db.stream_calls[-1]["filters"] == [("price", "gte", 40.0)]

    def test_in_requires_a_list(self, client):
        r = client.post(
            "/api/v1/products/export",
            json={"filters": [{"column": "id", "op": "in", "value": 3}]},
        )
        assert r.status_code == 400 and "needs a list" in r.json()["detail"]

    def test_empty_body_exports_everything(self, client):
        assert client.post("/api/v1/products/export", json={}).json()["row_count"] == 5

    def test_invalid_operator_and_limit_are_422(self, client):
        bad_op = client.post(
            "/api/v1/products/export", json={"filters": [{"column": "id", "op": "between"}]}
        )
        assert bad_op.status_code == 422
        assert client.post("/api/v1/products/export", json={"limit": 0}).status_code == 422


class TestValidation:
    def test_unknown_field_400(self, client):
        r = client.get("/api/v1/products/export?fields=id,nope")
        assert r.status_code == 400 and "nope" in r.json()["detail"]

    def test_unknown_filter_column_400(self, client):
        assert client.get("/api/v1/products/export?filter[nope][eq]=1").status_code == 400

    def test_unknown_sort_column_400(self, client):
        assert client.get("/api/v1/products/export?sort=nope:asc").status_code == 400

    def test_unknown_format_422(self, client):
        assert client.get("/api/v1/products/export?format=csv").status_code == 422


class TestMaxRows:
    def test_cap_applies_and_is_announced(self, db):
        client = TestClient(_app(ExportConfig(max_rows=2), db=db))
        r = client.get("/api/v1/products/export")
        assert r.json()["row_count"] == 2
        assert r.headers["x-export-max-rows"] == "2"
        assert db.stream_calls[-1]["limit"] == 2

    def test_smaller_limit_is_not_capped(self, db):
        client = TestClient(_app(ExportConfig(max_rows=2), db=db))
        r = client.get("/api/v1/products/export?limit=1")
        assert r.json()["row_count"] == 1 and "x-export-max-rows" not in r.headers

    def test_larger_limit_is_capped(self, db):
        client = TestClient(_app(ExportConfig(max_rows=2), db=db))
        r = client.get("/api/v1/products/export?limit=10")
        assert r.json()["row_count"] == 2 and r.headers["x-export-max-rows"] == "2"

    def test_effective_limit_helper(self):
        assert effective_limit(None, 0) == (None, False)
        assert effective_limit(7, 0) == (7, False)
        assert effective_limit(None, 5) == (5, True)
        assert effective_limit(9, 5) == (5, True)
        assert effective_limit(3, 5) == (3, False)


class TestArrow:
    def test_round_trip_preserves_types(self, client):
        r = client.get("/api/v1/products/export?format=arrow")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/vnd.apache.arrow.stream")
        assert r.headers["x-export-format"] == "arrow"
        table = pa.ipc.open_stream(r.content).read_all()
        assert table.num_rows == 5
        assert table.schema.field("id").type == pa.int32()
        assert table.schema.field("price").type == pa.decimal128(10, 2)
        assert table.schema.field("active").type == pa.bool_()
        assert table.schema.field("created_at").type == pa.timestamp("us", tz="UTC")
        assert table.schema.field("payload").type == pa.binary()
        assert table.column("price").to_pylist()[0] == Decimal("10.50")
        assert table.column("payload").to_pylist()[2] is None
        assert table.column("created_at").to_pylist()[0] == datetime(2024, 1, 1, tzinfo=UTC)

    def test_projection_limits_schema(self, client):
        r = client.get("/api/v1/products/export?format=arrow&fields=name,id")
        table = pa.ipc.open_stream(r.content).read_all()
        assert table.schema.names == ["name", "id"]

    def test_empty_stream_has_schema_only(self, client):
        r = client.get("/api/v1/products/export?format=arrow&filter[id][gt]=100")
        table = pa.ipc.open_stream(r.content).read_all()
        assert table.num_rows == 0 and table.schema.names == PRODUCTS.get_column_names()

    def test_501_without_pyarrow(self, client, monkeypatch):
        monkeypatch.setattr(export_module, "arrow_available", lambda: False)
        r = client.get("/api/v1/products/export?format=arrow")
        assert r.status_code == 501 and "pyarrow" in r.json()["detail"]


class TestDisabledAndAuth:
    def test_disabled_returns_403_stub(self):
        client = TestClient(_app(ExportConfig(enabled=False)))
        assert client.get("/api/v1/products/export").status_code == 403
        assert client.post("/api/v1/products/export", json={}).status_code == 403

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
        c = TestClient(_app(auth_manager=manager, with_crud=False))
        assert c.get("/api/v1/products/export").status_code == 401
        assert c.get("/api/v1/products/export", headers={"X-API-Key": "writer"}).status_code == 403
        assert c.get("/api/v1/products/export", headers={"X-API-Key": "reader"}).status_code == 200
        assert (
            c.post("/api/v1/products/export", json={}, headers={"X-API-Key": "reader"}).status_code
            == 200
        )


def test_export_route_precedes_crud_get_by_id(client):
    # With CRUD mounted after the export router, /products/export is not
    # swallowed by GET /products/{id}; CRUD id parsing is still in place.
    assert client.get("/api/v1/products/export").status_code == 200
    assert client.get("/api/v1/products/abc").status_code == 422


class TestExportRowSecurity:
    """An export reads far more rows than a list call, so it must be scoped too."""

    def _client(self) -> TestClient:
        auth = AuthManager(
            AuthConfig(
                enabled=True,
                api_keys=[
                    ApiKeyConfig(
                        key="odd-key",
                        name="odd",
                        permissions=["all"],
                        # `active` is True for the odd-numbered rows.
                        row_filters={"products": [{"column": "active", "value": True}]},
                    ),
                    ApiKeyConfig(key="root-key", name="root", permissions=["all"]),
                ],
            )
        )
        return TestClient(_app(auth_manager=auth, with_crud=False))

    def test_get_export_is_scoped(self):
        client = self._client()
        restricted = client.get("/api/v1/products/export", headers={"X-API-Key": "odd-key"})
        assert restricted.status_code == 200
        assert restricted.json()["row_count"] == 3

        unrestricted = client.get("/api/v1/products/export", headers={"X-API-Key": "root-key"})
        assert unrestricted.json()["row_count"] == 5

    def test_post_export_is_scoped(self):
        client = self._client()
        restricted = client.post(
            "/api/v1/products/export", json={}, headers={"X-API-Key": "odd-key"}
        )
        assert restricted.status_code == 200
        assert restricted.json()["row_count"] == 3

    def test_a_body_filter_cannot_reach_past_the_policy(self):
        client = self._client()
        response = client.post(
            "/api/v1/products/export",
            json={"filters": [{"column": "active", "op": "eq", "value": False}]},
            headers={"X-API-Key": "odd-key"},
        )
        # Both conditions are ANDed, so asking for the other half yields none.
        assert response.json()["row_count"] == 0

    def test_ndjson_is_scoped_as_well(self):
        client = self._client()
        response = client.get(
            "/api/v1/products/export?format=ndjson", headers={"X-API-Key": "odd-key"}
        )
        lines = [line for line in response.text.splitlines() if line.strip()]
        assert len(lines) == 3

    def test_the_policy_survives_a_projection(self):
        # Excluding the policy column from `fields` must not drop the condition:
        # it lives in the WHERE clause, not the SELECT list.
        client = self._client()
        response = client.get(
            "/api/v1/products/export?fields=id,name", headers={"X-API-Key": "odd-key"}
        )
        body = response.json()
        assert body["row_count"] == 3
        assert set(body["items"][0]) == {"id", "name"}


class TestExportMasking:
    """An export is the bulk path, so a forgotten mask leaks the whole table."""

    def _client(self) -> TestClient:
        auth = AuthManager(
            AuthConfig(
                enabled=True,
                api_keys=[
                    ApiKeyConfig(key="plain-key", name="plain", permissions=["all"]),
                    ApiKeyConfig(
                        key="admin-key", name="admin", permissions=["all"], roles=["admin"]
                    ),
                ],
            )
        )
        masking = CatalogMasking(
            policy=MaskingPolicy(rules={"name": "redact"}, exempt_roles=("admin",)),
            semantic_types={"products": {"name": "name"}},
        )
        return TestClient(_app(auth_manager=auth, with_crud=False, masking=masking))

    def test_json_export_is_masked(self):
        items = (
            self._client()
            .get("/api/v1/products/export", headers={"X-API-Key": "plain-key"})
            .json()["items"]
        )
        assert {item["name"] for item in items} == {"***"}
        # Unlabelled columns are untouched.
        assert items[0]["id"] == 1

    def test_ndjson_export_is_masked(self):
        text = (
            self._client()
            .get("/api/v1/products/export?format=ndjson", headers={"X-API-Key": "plain-key"})
            .text
        )
        assert '"name": "***"' in text or '"name":"***"' in text
        assert "product 1" not in text

    def test_arrow_export_is_masked(self):
        pa = pytest.importorskip("pyarrow")
        response = self._client().get(
            "/api/v1/products/export?format=arrow", headers={"X-API-Key": "plain-key"}
        )
        assert response.status_code == 200
        table = pa.ipc.open_stream(response.content).read_all()
        assert set(table.column("name").to_pylist()) == {"***"}

    def test_an_exempt_role_exports_the_real_values(self):
        items = (
            self._client()
            .get("/api/v1/products/export", headers={"X-API-Key": "admin-key"})
            .json()["items"]
        )
        assert "product 1" in {item["name"] for item in items}
