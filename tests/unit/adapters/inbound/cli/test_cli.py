"""Tests for the Warp Catalog CLI (click commands).

Uses CliRunner with a temp config file whose catalog.storage_path points at a
tmp dir seeded with a catalog. Store-only commands (list, info, export, review
--auto-approve, enrich-openapi) are exercised end-to-end; analyze/pipeline are
covered only for early/argument paths.
"""

from pathlib import Path

import yaml
from click.testing import CliRunner

from warp.adapters.inbound.cli.main import main
from warp.adapters.outbound.catalog_store.file_store import CatalogFileStore
from warp.application.services.catalog_review import CatalogReviewService
from warp.domain.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)


def _seed_catalog(storage_path: Path) -> CatalogFileStore:
    store = CatalogFileStore(storage_path)
    catalog = DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        description=LocalizedText(texts={"en": "A test db"}),
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="integer", is_primary_key=True),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        is_foreign_key=False,
                        tags=["pii"],
                        description=LocalizedText(texts={"en": "Email address"}),
                    ),
                ],
                primary_key="id",
                row_count=42,
                tags=["core"],
            ),
        },
        languages=["en"],
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )
    CatalogReviewService(store).save_as_draft(catalog)
    return store


def _write_config(tmp_path: Path, storage_path: Path) -> Path:
    config = {
        "databases": [
            {
                "name": "testdb",
                "type": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "testdb",
                "username": "u",
                "password": "p",
            }
        ],
        "settings": {"catalog": {"storage_path": str(storage_path)}},
    }
    config_file = tmp_path / "database.yaml"
    config_file.write_text(yaml.dump(config))
    return config_file


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0


def test_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.output


class TestListCommand:
    def test_list_catalogs(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "list"])
        assert result.exit_code == 0
        assert "testdb" in result.output
        assert "1 tables" in result.output
        assert "status=draft" in result.output

    def test_list_empty(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "list"])
        assert result.exit_code == 0
        assert "No catalogs found" in result.output


class TestInfoCommand:
    def test_info(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "info", "-d", "testdb"])
        assert result.exit_code == 0
        assert "Database: testdb" in result.output
        assert "Tables: 1" in result.output
        assert "Users (users)" in result.output
        assert "42 rows" in result.output

    def test_info_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "info", "-d", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestExportCommand:
    def test_export_json(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        out = tmp_path / "out.json"
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "export", "-d", "testdb", "-o", str(out), "-f", "json"]
        )
        assert result.exit_code == 0
        assert out.exists()
        assert "Exported 1 tables" in result.output

    def test_export_markdown(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        out = tmp_path / "out.md"
        result = CliRunner().invoke(
            main,
            ["-c", str(cfg), "export", "-d", "testdb", "-o", str(out), "-f", "markdown"],
        )
        assert result.exit_code == 0
        assert out.exists()
        assert "# testdb" in out.read_text()

    def test_export_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        out = tmp_path / "out.json"
        result = CliRunner().invoke(main, ["-c", str(cfg), "export", "-d", "ghost", "-o", str(out)])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestReviewCommand:
    def test_review_auto_approve(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "review", "-d", "testdb", "--auto-approve"]
        )
        assert result.exit_code == 0
        assert "auto-approved" in result.output
        assert store.load_or_raise("testdb").status.value == "approved"

    def test_review_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_review_interactive_approve_all(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        # single table -> answer "a" to approve it, then it auto-approves catalog
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="a\n")
        assert result.exit_code == 0
        assert store.load_or_raise("testdb").status.value == "approved"

    def test_review_interactive_quit(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="q\n")
        assert result.exit_code == 0
        assert "paused" in result.output
        assert store.load_or_raise("testdb").status.value == "draft"

    def test_review_already_approved_decline_reopen(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        CatalogReviewService(store).approve_catalog("testdb")
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="n\n")
        assert result.exit_code == 0
        assert "already approved" in result.output


class TestEnrichOpenapiCommand:
    def test_enrich_openapi(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        spec = tmp_path / "spec.json"
        spec.write_text('{"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": {}}')
        out = tmp_path / "enriched.json"
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(cfg),
                "enrich-openapi",
                "-d",
                "testdb",
                "-i",
                str(spec),
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert out.exists()

    def test_enrich_openapi_catalog_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        spec = tmp_path / "spec.json"
        spec.write_text('{"openapi": "3.0.0", "paths": {}}')
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "enrich-openapi", "-d", "ghost", "-i", str(spec)]
        )
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAnalyzeEarlyPath:
    def test_analyze_db_config_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "analyze", "-d", "missing_db"])
        assert result.exit_code == 1
        assert "not found" in result.output
