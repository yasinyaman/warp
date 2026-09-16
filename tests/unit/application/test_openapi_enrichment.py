"""Tests for integration modules (OpenAPIEnricher, MCPEnricher)."""

import json

import pytest

from warp.application.services.openapi_enrichment import OpenAPIEnricher
from warp.domain.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)


@pytest.fixture
def catalog():
    return DatabaseCatalog(
        database_name="testdb",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                        semantic_type="id",
                        description=LocalizedText(texts={"en": "User ID"}),
                    ),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        description=LocalizedText(texts={"en": "User email"}),
                    ),
                ],
                primary_key="id",
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="user_id",
                        relationship_type="one-to-many",
                        description=LocalizedText(texts={"en": "User has orders"}),
                    )
                ],
            ),
        },
    )


class TestOpenAPIEnricher:
    def test_enrich_basic(self, catalog):
        spec = {
            "paths": {
                "/api/v1/users": {
                    "get": {
                        "summary": "List users",
                        "parameters": [],
                    },
                },
                "/api/v1/users/{id}": {
                    "get": {
                        "summary": "Get user",
                        "parameters": [
                            {"name": "id", "in": "path"},
                        ],
                    },
                },
            },
            "components": {"schemas": {}},
        }

        enricher = OpenAPIEnricher(catalog, lang="en")
        result = enricher.enrich(spec)

        # Should enrich summary
        get_op = result["paths"]["/api/v1/users"]["get"]
        assert "Users" in get_op["summary"]
        # Description is now a rich markdown block led by the table summary.
        assert get_op["description"].startswith("**Users**: User accounts")

    def test_enrich_request_body(self, catalog):
        spec = {
            "paths": {
                "/api/v1/users": {
                    "post": {
                        "summary": "Create user",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "properties": {
                                            "email": {"type": "string"},
                                        }
                                    }
                                }
                            }
                        },
                    },
                },
            },
        }

        enricher = OpenAPIEnricher(catalog, lang="en")
        result = enricher.enrich(spec)

        body_schema = result["paths"]["/api/v1/users"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        # Column descriptions now include semantic-type metadata.
        email_desc = body_schema["properties"]["email"]["description"]
        assert email_desc.startswith("User email")
        assert "Semantic type: email" in email_desc

    def test_extract_table_from_path(self, catalog):
        enricher = OpenAPIEnricher(catalog)
        assert enricher._extract_table_from_path("/api/v1/users") == "users"
        assert enricher._extract_table_from_path("/api/v1/users/{id}") == "users"
        # Only /api/v1-prefixed paths are matched; unprefixed paths are ignored.
        assert enricher._extract_table_from_path("/users") is None

    def test_extract_column_from_param(self):
        assert OpenAPIEnricher._extract_column_from_param("filter[email][eq]") == "email"
        assert OpenAPIEnricher._extract_column_from_param("sort") is None
        assert OpenAPIEnricher._extract_column_from_param("email") == "email"
        assert OpenAPIEnricher._extract_column_from_param("limit") is None

    def test_enrich_file(self, catalog, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/api/v1/users": {
                    "get": {"summary": "List", "parameters": []},
                },
            },
        }
        input_file = tmp_path / "spec.json"
        with open(input_file, "w") as f:
            json.dump(spec, f)

        enricher = OpenAPIEnricher(catalog, lang="en")
        output_file = enricher.enrich_file(input_file)

        assert output_file.exists()
        with open(output_file) as f:
            result = json.load(f)
        assert "Users" in result["paths"]["/api/v1/users"]["get"]["summary"]


class TestOpenAPIExamplesPolicy:
    """Sample values are only written into the spec when explicitly enabled."""

    def _spec(self):
        return {
            "paths": {
                "/api/v1/users": {
                    "get": {
                        "summary": "List users",
                        "parameters": [{"name": "filter[email]", "in": "query"}],
                    },
                    "post": {
                        "summary": "Create user",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"properties": {"email": {"type": "string"}}}
                                }
                            }
                        },
                    },
                }
            },
            "components": {
                "schemas": {
                    "warp__schema__analyzer__UsersResponse": {
                        "properties": {"email": {"type": "string"}}
                    }
                }
            },
        }

    @staticmethod
    def _find_keys(obj, keys):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    found.append((k, v))
                found.extend(TestOpenAPIExamplesPolicy._find_keys(v, keys))
        elif isinstance(obj, list):
            for v in obj:
                found.extend(TestOpenAPIExamplesPolicy._find_keys(v, keys))
        return found

    @staticmethod
    def _with_samples(catalog):
        col = catalog.tables["users"].get_column("email")
        assert col is not None
        col.sample_values = ["a@x.com", "b@x.com"]
        return catalog

    def test_no_examples_by_default(self, catalog):
        result = OpenAPIEnricher(self._with_samples(catalog), lang="en").enrich(self._spec())
        assert self._find_keys(result, {"example", "examples"}) == []
        assert "Examples:" not in json.dumps(result)
        # Descriptions are still injected.
        assert "x-llm-context" in result

    def test_examples_when_enabled(self, catalog):
        result = OpenAPIEnricher(
            self._with_samples(catalog), lang="en", include_examples=True
        ).enrich(self._spec())
        found = self._find_keys(result, {"example", "examples"})
        assert found != []
        assert any("a@x.com" in json.dumps(v) for _, v in found)


class TestMultiDatabaseContext:
    """Several catalogs enrich one spec into a single, merged x-llm-context."""

    @staticmethod
    def _catalog(name: str) -> DatabaseCatalog:
        return DatabaseCatalog(
            database_name=name,
            tables={"users": TableCatalogEntry(table_name="users")},
        )

    def test_single_catalog_keeps_flat_shape(self):
        spec = OpenAPIEnricher(self._catalog("a")).enrich({"paths": {}})
        assert spec["x-llm-context"]["database"] == "a"
        assert "databases" not in spec["x-llm-context"]

    def test_two_catalogs_are_merged_not_overwritten(self):
        spec = {"paths": {}}
        OpenAPIEnricher(self._catalog("a")).enrich(spec)
        OpenAPIEnricher(self._catalog("b")).enrich(spec)
        ctx = spec["x-llm-context"]
        assert [d["database"] for d in ctx["databases"]] == ["a", "b"]
        assert "database" not in ctx

    def test_re_enriching_same_database_replaces_its_entry(self):
        spec = {"paths": {}}
        OpenAPIEnricher(self._catalog("a")).enrich(spec)
        OpenAPIEnricher(self._catalog("b")).enrich(spec)
        OpenAPIEnricher(self._catalog("a")).enrich(spec)
        assert [d["database"] for d in spec["x-llm-context"]["databases"]] == ["b", "a"]
        spec2 = {"paths": {}}
        OpenAPIEnricher(self._catalog("a")).enrich(spec2)
        OpenAPIEnricher(self._catalog("a")).enrich(spec2)
        assert spec2["x-llm-context"]["database"] == "a"  # still flat, no duplicate
