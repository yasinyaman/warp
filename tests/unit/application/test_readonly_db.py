"""Tests for the optional read-only connection used by the raw SQL endpoint."""

from typing import Any

from warp.application.config import DatabaseConfig


def _cfg(**extra: Any) -> DatabaseConfig:
    base: dict[str, Any] = {
        "name": "primary",
        "type": "postgresql",
        "database": "db",
        "username": "rw",
        "password": "rwpass",
    }
    base.update(extra)
    return DatabaseConfig(**base)


def test_no_readonly_returns_none():
    assert _cfg().readonly_config() is None


def test_readonly_config_swaps_credentials():
    ro = _cfg(readonly_username="ro", readonly_password="ropass").readonly_config()
    assert ro is not None
    assert ro["username"] == "ro"
    assert ro["password"] == "ropass"
    assert ro["name"] == "primary__readonly"
    # The connection target is unchanged — only credentials differ.
    assert ro["database"] == "db"
    assert ro["type"] == "postgresql"


def test_readonly_password_optional():
    # username set, empty password (e.g. peer/trust auth) still activates.
    ro = _cfg(readonly_username="ro").readonly_config()
    assert ro is not None
    assert ro["username"] == "ro"
    assert ro["password"] == ""
