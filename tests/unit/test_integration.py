"""Tests for integration modules (OpenAPIEnricher, MCPEnricher)."""

import json
from unittest.mock import MagicMock

import pytest

from warp.catalog.models import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)
from warp.integration.mcp_enricher import MCPEnricher
from warp.integration.openapi_enricher import OpenAPIEnricher


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


class TestMCPEnricher:
    def test_enrich_tool(self, catalog):
        tool = MagicMock()
        tool.name = "list_users"
        tool.http_path = "/api/v1/users"
        tool.description = "List all users"
        tool.input_schema = {"properties": {"email": {"type": "string"}}}

        enricher = MCPEnricher(catalog, lang="en")
        result = enricher._enrich_tool(tool)

        assert result is True
        assert "User accounts" in tool.description
        assert "email(email)" in tool.description

    def test_enrich_resource(self, catalog):
        resource = MagicMock()
        resource.name = "users"
        resource.uri = "/api/v1/users"
        resource.description = "Users resource"

        enricher = MCPEnricher(catalog, lang="en")
        result = enricher._enrich_resource(resource)

        assert result is True
        assert "User accounts" in resource.description

    def test_enrich_server(self, catalog):
        tool = MagicMock()
        tool.name = "list_users"
        tool.http_path = "/api/v1/users"
        tool.description = "List users"
        tool.input_schema = {"properties": {}}

        server = MagicMock()
        server.tools = [tool]
        server.resources = []

        enricher = MCPEnricher(catalog, lang="en")
        enricher.enrich(server)

        assert "User accounts" in tool.description

    def test_extract_table_name(self):
        assert MCPEnricher._extract_table_name("/api/v1/users", "") == "users"
        assert MCPEnricher._extract_table_name("", "list_users") == "users"
        assert MCPEnricher._extract_table_name("", "get_orders_by_id") == "orders"

    def test_no_match(self, catalog):
        tool = MagicMock()
        tool.name = "unknown_action"
        tool.http_path = "/api/v1/nonexistent"
        tool.description = "Something"
        tool.input_schema = {}

        enricher = MCPEnricher(catalog, lang="en")
        result = enricher._enrich_tool(tool)

        assert result is False
