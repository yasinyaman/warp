"""End-to-end tests for the catalog REST API router.

Drives create_catalog_router with a real CatalogFileStore (tmp_path) and a
seeded DatabaseCatalog via fastapi.testclient.TestClient. The /analyze endpoint
(which needs an LLM) is exercised only for its early error paths.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from warp.api.auth import AuthManager
from warp.api.catalog_router import create_catalog_router
from warp.catalog.models import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)
from warp.catalog.store import CatalogFileStore
from warp.config.settings import ApiKeyConfig, AuthConfig, DatabaseConfig, Settings


def _make_catalog() -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        description=LocalizedText(texts={"en": "Test database", "tr": "Test veritabani"}),
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users", "tr": "Kullanicilar"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                        description=LocalizedText(texts={"en": "Primary key"}),
                    ),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        sample_values=["a@x.com"],
                        description=LocalizedText(texts={"en": "Email"}),
                    ),
                ],
                primary_key="id",
                row_count=1000,
                tags=["core"],
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="user_id",
                        relationship_type="one-to-many",
                        description=LocalizedText(texts={"en": "User orders"}),
                    )
                ],
            ),
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Customer orders"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="integer", is_primary_key=True),
                    ColumnCatalogEntry(
                        name="user_id",
                        data_type="integer",
                        is_foreign_key=True,
                        references="users.id",
                    ),
                ],
                primary_key="id",
            ),
        },
        languages=["en", "tr"],
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )


@pytest.fixture
def store(tmp_path: Path) -> CatalogFileStore:
    s = CatalogFileStore(tmp_path)
    s.save_as_draft(_make_catalog())
    return s


@pytest.fixture
def client(store: CatalogFileStore) -> TestClient:
    app = FastAPI()
    router = create_catalog_router(store=store, config=Settings(), adapters={}, app=app)
    app.include_router(router)
    return TestClient(app)


class TestListAndInfo:
    def test_list_catalogs(self, client: TestClient) -> None:
        r = client.get("/catalog")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        names = [c["database_name"] for c in body["catalogs"]]
        assert "testdb" in names

    def test_list_catalogs_empty(self, tmp_path: Path) -> None:
        app = FastAPI()
        empty_store = CatalogFileStore(tmp_path / "empty")
        app.include_router(create_catalog_router(store=empty_store))
        c = TestClient(app)
        r = c.get("/catalog")
        assert r.status_code == 200
        assert r.json() == {"catalogs": [], "total": 0}

    def test_get_catalog_info(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb")
        assert r.status_code == 200
        body = r.json()
        assert body["database_name"] == "testdb"
        assert body["table_count"] == 2
        assert body["llm_provider"] == "openai"
        tables = {t["table_name"]: t for t in body["tables"]}
        assert tables["users"]["human_name"] == "Users"
        assert tables["users"]["column_count"] == 2

    def test_get_catalog_info_lang(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb", params={"lang": "tr"})
        assert r.status_code == 200
        tables = {t["table_name"]: t for t in r.json()["tables"]}
        assert tables["users"]["human_name"] == "Kullanicilar"

    def test_get_catalog_info_404(self, client: TestClient) -> None:
        r = client.get("/catalog/nope")
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]


class TestExport:
    def test_export_json(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "json"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "attachment" in r.headers["content-disposition"]
        assert r.json()["database_name"] == "testdb"

    def test_export_yaml(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "yaml"})
        assert r.status_code == 200
        assert "yaml" in r.headers["content-type"]
        assert "database_name: testdb" in r.text

    def test_export_markdown(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "markdown"})
        assert r.status_code == 200
        assert "markdown" in r.headers["content-type"]
        assert "# testdb" in r.text

    def test_export_bad_format(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "xml"})
        assert r.status_code == 400

    def test_export_404(self, client: TestClient) -> None:
        r = client.get("/catalog/nope/export")
        assert r.status_code == 404


class TestDraftReview:
    def test_get_draft_info(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/draft")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "draft"
        assert body["review_summary"].get("pending") == 2
        assert len(body["tables"]) == 2

    def test_get_draft_info_404(self, client: TestClient) -> None:
        assert client.get("/catalog/nope/draft").status_code == 404

    def test_get_draft_table(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/draft/tables/users")
        assert r.status_code == 200
        body = r.json()
        assert body["table_name"] == "users"
        assert body["column_count"] == 2
        assert len(body["relationships"]) == 1
        cols = {c["name"]: c for c in body["columns"]}
        assert cols["email"]["semantic_type"] == "email"

    def test_get_draft_table_catalog_404(self, client: TestClient) -> None:
        assert client.get("/catalog/nope/draft/tables/users").status_code == 404

    def test_get_draft_table_table_404(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/draft/tables/ghost")
        assert r.status_code == 404


class TestEdit:
    def test_edit_table(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/testdb/draft/tables/users",
            json={"description": {"en": "Edited desc"}, "tags": ["a", "b"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["review_status"] == "modified"
        assert set(body["updated_fields"]) == {"description", "tags"}
        # Verify persisted
        detail = client.get("/catalog/testdb/draft/tables/users").json()
        assert detail["description"]["en"] == "Edited desc"
        assert detail["tags"] == ["a", "b"]

    def test_edit_table_no_fields(self, client: TestClient) -> None:
        r = client.patch("/catalog/testdb/draft/tables/users", json={})
        assert r.status_code == 400

    def test_edit_table_catalog_404(self, client: TestClient) -> None:
        r = client.patch("/catalog/nope/draft/tables/users", json={"tags": ["x"]})
        assert r.status_code == 404

    def test_edit_table_not_draft(self, client: TestClient, store: CatalogFileStore) -> None:
        store.approve_catalog("testdb")
        r = client.patch("/catalog/testdb/draft/tables/users", json={"tags": ["x"]})
        assert r.status_code == 409

    def test_edit_table_unknown_table(self, client: TestClient) -> None:
        r = client.patch("/catalog/testdb/draft/tables/ghost", json={"tags": ["x"]})
        assert r.status_code == 400

    def test_edit_column(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/testdb/draft/tables/users/columns/email",
            json={"semantic_type": "contact_email", "description": {"en": "the email"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["column_name"] == "email"
        assert body["semantic_type"] == "contact_email"

    def test_edit_column_no_fields(self, client: TestClient) -> None:
        r = client.patch("/catalog/testdb/draft/tables/users/columns/email", json={})
        assert r.status_code == 400

    def test_edit_column_catalog_404(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/nope/draft/tables/users/columns/email",
            json={"semantic_type": "x"},
        )
        assert r.status_code == 404

    def test_edit_column_not_draft(self, client: TestClient, store: CatalogFileStore) -> None:
        store.approve_catalog("testdb")
        r = client.patch(
            "/catalog/testdb/draft/tables/users/columns/email",
            json={"semantic_type": "x"},
        )
        assert r.status_code == 409

    def test_edit_column_unknown(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/testdb/draft/tables/users/columns/ghost",
            json={"semantic_type": "x"},
        )
        assert r.status_code == 400


class TestApprove:
    def test_approve_specific_tables(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve", json={"tables": ["users"]})
        assert r.status_code == 200
        body = r.json()
        # only one of two approved -> catalog stays draft
        assert body["status"] == "draft"
        assert body["review_summary"].get("approved") == 1

    def test_approve_all(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_approve_specific_then_all(self, client: TestClient) -> None:
        client.post("/catalog/testdb/approve", json={"tables": ["users"]})
        r = client.post("/catalog/testdb/approve", json={"tables": ["orders"]})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_approve_404(self, client: TestClient) -> None:
        r = client.post("/catalog/nope/approve", json={})
        assert r.status_code == 404

    def test_approve_unknown_table_400(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve", json={"tables": ["ghost"]})
        assert r.status_code == 400

    def test_approve_single_table(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve/users")
        assert r.status_code == 200
        body = r.json()
        assert body["table"] == "users"
        assert body["catalog_status"] == "draft"

    def test_approve_single_table_completes_catalog(self, client: TestClient) -> None:
        client.post("/catalog/testdb/approve/users")
        r = client.post("/catalog/testdb/approve/orders")
        assert r.status_code == 200
        assert r.json()["catalog_status"] == "approved"

    def test_approve_single_table_404(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve/ghost")
        assert r.status_code == 400


class TestDelete:
    def test_delete(self, client: TestClient) -> None:
        r = client.delete("/catalog/testdb")
        assert r.status_code == 200
        assert r.json() == {"deleted": True, "database": "testdb"}
        assert client.get("/catalog/testdb").status_code == 404

    def test_delete_404(self, client: TestClient) -> None:
        assert client.delete("/catalog/nope").status_code == 404


class TestOpenAPIEnrichmentRefresh:
    """Cover _refresh_openapi_enrichment via the approve endpoints."""

    def _client_with_enrichment(self, store: CatalogFileStore) -> TestClient:
        app = FastAPI(title="t", version="1.0.0")
        config = Settings()
        config.settings.catalog.auto_enrich_openapi = True
        router = create_catalog_router(
            store=store,
            config=config,
            adapters={"testdb": object()},
            app=app,
        )
        app.include_router(router)
        return TestClient(app)

    def test_approve_all_triggers_refresh(self, store: CatalogFileStore) -> None:
        client = self._client_with_enrichment(store)
        r = client.post("/catalog/testdb/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
        # OpenAPI schema is now enriched/cached
        assert client.get("/openapi.json").status_code == 200

    def test_approve_single_completes_and_refreshes(self, store: CatalogFileStore) -> None:
        client = self._client_with_enrichment(store)
        client.post("/catalog/testdb/approve/users")
        r = client.post("/catalog/testdb/approve/orders")
        assert r.status_code == 200
        assert r.json()["catalog_status"] == "approved"

    def test_refresh_disabled_by_config(self, store: CatalogFileStore) -> None:
        app = FastAPI()
        config = Settings()
        config.settings.catalog.auto_enrich_openapi = False
        router = create_catalog_router(
            store=store, config=config, adapters={"testdb": object()}, app=app
        )
        app.include_router(router)
        client = TestClient(app)
        r = client.post("/catalog/testdb/approve", json={})
        assert r.status_code == 200


class TestAnalyzeEarlyPaths:
    """Cover /analyze guards without invoking the LLM."""

    def test_analyze_no_adapters(self, store: CatalogFileStore) -> None:
        app = FastAPI()
        # config present but adapters empty -> 503
        app.include_router(create_catalog_router(store=store, config=Settings(), adapters=None))
        c = TestClient(app)
        r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 503

    def test_analyze_db_not_connected(self, store: CatalogFileStore) -> None:
        app = FastAPI()
        app.include_router(
            create_catalog_router(store=store, config=Settings(), adapters={"other": object()})
        )
        c = TestClient(app)
        r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 404

    def test_analyze_db_config_missing(self, store: CatalogFileStore) -> None:
        # adapter named "testdb" exists, but no matching db in config.databases
        app = FastAPI()
        app.include_router(
            create_catalog_router(store=store, config=Settings(), adapters={"testdb": object()})
        )
        c = TestClient(app)
        r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 404
        assert "config not found" in r.json()["detail"]


class TestCatalogNamePathSafety:
    """`{db_name}` path params are validated before they can reach the filesystem."""

    def test_delete_traversal_rejected(self, client: TestClient, store: CatalogFileStore) -> None:
        sibling = store.base_path.parent / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("x")

        r = client.delete("/catalog/%2e%2e")  # decodes to ".."
        assert r.status_code == 422

        assert store.base_path.exists()
        assert (sibling / "keep.txt").exists()
        assert client.get("/catalog/testdb").status_code == 200

    @pytest.mark.parametrize("name", ["%2e%2e", "_index", "a.b", "bad%20name"])
    def test_read_endpoints_reject_unsafe_names(self, client: TestClient, name: str) -> None:
        assert client.get(f"/catalog/{name}").status_code == 422
        assert client.get(f"/catalog/{name}/draft").status_code == 422
        assert client.post(f"/catalog/{name}/approve").status_code == 422


def _auth_manager() -> AuthManager:
    return AuthManager(
        AuthConfig(
            enabled=True,
            api_keys=[
                ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
                ApiKeyConfig(key="writer-key", name="writer", permissions=["create", "update"]),
                ApiKeyConfig(key="admin-key", name="admin", permissions=["all"]),
            ],
            public_paths=["/health"],
        )
    )


@pytest.fixture
def auth_client(store: CatalogFileStore) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_catalog_router(
            store=store, config=Settings(), adapters={}, app=app, auth_manager=_auth_manager()
        )
    )
    return TestClient(app)


def _h(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


class TestAuth:
    """Every catalog endpoint is behind the auth manager when auth is enabled."""

    def test_no_key_is_401(self, auth_client: TestClient) -> None:
        assert auth_client.get("/catalog").status_code == 401
        assert auth_client.get("/catalog/testdb").status_code == 401
        assert auth_client.delete("/catalog/testdb").status_code == 401
        assert auth_client.post("/catalog/analyze", json={"database": "testdb"}).status_code == 401

    def test_invalid_key_is_401(self, auth_client: TestClient) -> None:
        assert auth_client.get("/catalog", headers=_h("nope")).status_code == 401

    def test_reader_can_only_read(self, auth_client: TestClient) -> None:
        h = _h("reader-key")
        assert auth_client.get("/catalog", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb/draft", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb/draft/tables/users", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb/export?format=json", headers=h).status_code == 200

        assert (
            auth_client.patch(
                "/catalog/testdb/draft/tables/users", json={"tags": ["core"]}, headers=h
            ).status_code
            == 403
        )
        assert auth_client.post("/catalog/testdb/approve", headers=h).status_code == 403
        assert auth_client.post("/catalog/testdb/approve/users", headers=h).status_code == 403
        assert auth_client.delete("/catalog/testdb", headers=h).status_code == 403
        assert (
            auth_client.post("/catalog/analyze", json={"database": "testdb"}, headers=h).status_code
            == 403
        )

    def test_writer_can_edit_and_analyze_but_not_delete(self, auth_client: TestClient) -> None:
        h = _h("writer-key")
        r = auth_client.patch(
            "/catalog/testdb/draft/tables/users", json={"tags": ["core"]}, headers=h
        )
        assert r.status_code == 200
        assert auth_client.post("/catalog/testdb/approve/users", headers=h).status_code == 200
        # Auth passes for CREATE; the handler then reports that no adapters are wired (503).
        assert (
            auth_client.post("/catalog/analyze", json={"database": "testdb"}, headers=h).status_code
            == 503
        )
        assert auth_client.delete("/catalog/testdb", headers=h).status_code == 403

    def test_admin_can_delete(self, auth_client: TestClient) -> None:
        assert auth_client.delete("/catalog/testdb", headers=_h("admin-key")).status_code == 200
        assert auth_client.get("/catalog/testdb", headers=_h("admin-key")).status_code == 404

    def test_auth_disabled_keeps_endpoints_open(self, client: TestClient) -> None:
        assert client.get("/catalog").status_code == 200


class TestAnalyzeErrorBodies:
    def test_unexpected_error_is_generic(self, store: CatalogFileStore) -> None:
        from unittest.mock import patch

        settings = Settings(
            databases=[DatabaseConfig(name="testdb", type="postgresql", database="x", username="u")]
        )
        app = FastAPI()
        app.include_router(
            create_catalog_router(store=store, config=settings, adapters={"testdb": object()})
        )
        c = TestClient(app)
        with patch(
            "warp.llm.client.LLMClient.from_config",
            side_effect=RuntimeError("secret internal detail: /etc/passwd"),
        ):
            r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 500
        assert r.json() == {"detail": "Analysis failed"}
        assert "secret" not in r.text
