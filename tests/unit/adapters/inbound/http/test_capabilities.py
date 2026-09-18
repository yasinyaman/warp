"""The /info capabilities block mirrors the effective settings."""

from warp.adapters.inbound.http.capabilities import capabilities_of
from warp.application.config import ExportConfig, SettingsConfig


def test_defaults_when_settings_missing():
    caps = capabilities_of(None, arrow_available=False)
    assert caps["api_prefix"] == "/api/v1"
    assert caps["db_prefix"] == "always"
    assert caps["schema"] is True
    assert caps["export"] == {
        "enabled": True,
        "formats": ["json", "ndjson"],
        "max_rows": 0,
        "batch_size": 5000,
    }
    assert caps["raw_query"] is False
    assert caps["filter_ops"] == ["eq", "gt", "gte", "in", "is_null", "like", "lt", "lte", "ne"]


def test_reports_arrow_only_when_available():
    assert "arrow" not in capabilities_of(None, arrow_available=False)["export"]["formats"]
    assert capabilities_of(None, arrow_available=True)["export"]["formats"] == [
        "json",
        "ndjson",
        "arrow",
    ]


def test_mirrors_export_config_and_raw_query():
    cfg = SettingsConfig(
        api_prefix="/v2",
        enable_raw_query=True,
        export=ExportConfig(enabled=False, max_rows=10_000, batch_size=250),
    )
    caps = capabilities_of(cfg, arrow_available=False)
    assert caps["api_prefix"] == "/v2"
    assert caps["raw_query"] is True
    assert caps["export"]["enabled"] is False
    assert caps["export"]["max_rows"] == 10_000
    assert caps["export"]["batch_size"] == 250
