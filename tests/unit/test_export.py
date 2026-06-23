"""Tests for catalog exporters."""

import json

import pytest
import yaml

from warp.catalog.models import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    IndexInfo,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)
from warp.export.json_exporter import JsonExporter
from warp.export.markdown_exporter import MarkdownExporter, get_exporter
from warp.export.yaml_exporter import YamlExporter


@pytest.fixture
def sample_catalog():
    return DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        description=LocalizedText(texts={"en": "Test database", "tr": "Test veritabanı"}),
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(
                    texts={"en": "User accounts", "tr": "Kullanıcı hesapları"}
                ),
                human_name=LocalizedText(texts={"en": "Users", "tr": "Kullanıcılar"}),
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
                        description=LocalizedText(
                            texts={"en": "Email address", "tr": "E-posta adresi"}
                        ),
                        sample_values=["alice@ex.com"],
                    ),
                ],
                primary_key="id",
                indexes=[IndexInfo(name="idx_email", columns=["email"], unique=True)],
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="user_id",
                        relationship_type="one-to-many",
                        description=LocalizedText(texts={"en": "User has orders"}),
                    )
                ],
                row_count=1000,
                tags=["auth", "core"],
            ),
        },
        languages=["en", "tr"],
    )


class TestJsonExporter:
    def test_export_string_all_languages(self, sample_catalog):
        exporter = JsonExporter()
        result = exporter.export_string(sample_catalog)
        data = json.loads(result)
        assert data["database_name"] == "testdb"
        assert "users" in data["tables"]

    def test_export_string_filtered_language(self, sample_catalog):
        exporter = JsonExporter()
        result = exporter.export_string(sample_catalog, lang="tr")
        data = json.loads(result)
        assert data["_language"] == "tr"
        assert data["description"] == "Test veritabanı"
        users = data["tables"]["users"]
        assert users["description"] == "Kullanıcı hesapları"
        assert users["columns"][1]["description"] == "E-posta adresi"

    def test_export_file(self, sample_catalog, tmp_path):
        exporter = JsonExporter()
        path = exporter.export(sample_catalog, tmp_path / "catalog.json")
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert data["database_name"] == "testdb"


class TestYamlExporter:
    def test_export_string(self, sample_catalog):
        exporter = YamlExporter()
        result = exporter.export_string(sample_catalog)
        data = yaml.safe_load(result)
        assert data["database_name"] == "testdb"

    def test_export_file(self, sample_catalog, tmp_path):
        exporter = YamlExporter()
        path = exporter.export(sample_catalog, tmp_path / "catalog.yaml")
        assert path.exists()


class TestMarkdownExporter:
    def test_export_string(self, sample_catalog):
        exporter = MarkdownExporter()
        result = exporter.export_string(sample_catalog, lang="en")
        assert "# testdb" in result
        assert "## Users" in result
        assert "User accounts" in result
        assert "| **id** (PK)" in result
        assert "email" in result
        assert "### Relationships" in result
        assert "### Indexes" in result

    def test_export_string_turkish(self, sample_catalog):
        exporter = MarkdownExporter()
        result = exporter.export_string(sample_catalog, lang="tr")
        assert "## Kullanıcılar" in result
        assert "Kullanıcı hesapları" in result
        assert "E-posta adresi" in result

    def test_export_file(self, sample_catalog, tmp_path):
        exporter = MarkdownExporter()
        path = exporter.export(sample_catalog, tmp_path / "catalog.md", lang="en")
        assert path.exists()
        content = path.read_text()
        assert "# testdb" in content


class TestGetExporter:
    def test_get_json(self):
        exp = get_exporter("json")
        assert isinstance(exp, JsonExporter)

    def test_get_yaml(self):
        exp = get_exporter("yaml")
        assert isinstance(exp, YamlExporter)

    def test_get_markdown(self):
        exp = get_exporter("markdown")
        assert isinstance(exp, MarkdownExporter)

    def test_get_md(self):
        exp = get_exporter("md")
        assert isinstance(exp, MarkdownExporter)

    def test_get_unknown(self):
        with pytest.raises(ValueError, match="Unknown export format"):
            get_exporter("xml")
