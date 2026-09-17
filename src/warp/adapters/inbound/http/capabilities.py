"""Capability advertisement: the ``capabilities`` block of ``GET /info``.

Clients (for example Fusion) read this block once at connect time to decide
which endpoints to use — the typed schema endpoint, the streaming export
endpoint and its formats, whether raw SQL is available — and how routes are
laid out, instead of probing or guessing from the version number.
"""

from typing import Any

from warp.application.config import SettingsConfig
from warp.domain.filtering import FilterParser


def capabilities_of(cfg: SettingsConfig | None, arrow_available: bool) -> dict[str, Any]:
    """Describe what this server offers, from its effective settings.

    Args:
        cfg: The loaded settings, or None before the lifespan has run (the
            defaults are reported in that case).
        arrow_available: Whether ``pyarrow`` is importable, which decides if
            ``arrow`` is listed among the export formats.

    Returns:
        A JSON-serializable mapping; keys are stable and additive across versions.
    """
    settings = cfg or SettingsConfig()
    formats = ["json", "ndjson"]
    if arrow_available:
        formats.append("arrow")
    return {
        "api_prefix": settings.api_prefix,
        # Every database is reachable at {api_prefix}/{db}/...; a single
        # configured database additionally answers at the bare prefix.
        "db_prefix": "always",
        "schema": True,
        "export": {
            "enabled": settings.export.enabled,
            "formats": formats,
            "max_rows": settings.export.max_rows,
            "batch_size": settings.export.batch_size,
        },
        "raw_query": settings.enable_raw_query,
        "filter_ops": sorted(FilterParser.OPERATORS),
    }
