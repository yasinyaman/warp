"""Tests for API-key authentication, RBAC, and public paths."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from warp.api.auth import AuthenticatedUser, AuthManager, Permission
from warp.config.settings import ApiKeyConfig, AuthConfig


def _auth_config(enabled=True):
    return AuthConfig(
        enabled=enabled,
        header_name="X-API-Key",
        api_keys=[
            ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
            ApiKeyConfig(key="admin-key", name="admin", permissions=["all"]),
            ApiKeyConfig(key="", name="empty", permissions=["all"]),  # ignored
        ],
        public_paths=["/health"],
    )


def _make_client(enabled=True):
    manager = AuthManager(_auth_config(enabled))
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/items", dependencies=[Depends(manager.require(Permission.READ))])
    async def list_items():
        return {"items": []}

    @app.post("/items", dependencies=[Depends(manager.require(Permission.CREATE))])
    async def create_item():
        return {"created": True}

    return TestClient(app), manager


class TestKeyStorage:
    def test_empty_keys_are_ignored(self):
        manager = AuthManager(_auth_config())
        assert manager.api_key_count == 2

    def test_no_plaintext_key_retained(self):
        manager = AuthManager(_auth_config())
        # Stored as sha256 hashes, not the plaintext key.
        assert all(h != "reader-key" for h, _ in manager._key_hashes)
        assert manager._get_api_key_config("reader-key").name == "reader"

    @pytest.mark.parametrize("bad", ["", None, "wrong-key", "reader-keys", "READER-KEY"])
    def test_invalid_keys_return_none(self, bad):
        manager = AuthManager(_auth_config())
        assert manager._get_api_key_config(bad) is None


class TestAuthenticatedUser:
    def test_all_permission_grants_everything(self):
        user = AuthenticatedUser(ApiKeyConfig(key="k", name="a", permissions=["all"]))
        assert user.has_permission(Permission.DELETE)
        assert not hasattr(user, "key")

    def test_specific_permission(self):
        user = AuthenticatedUser(ApiKeyConfig(key="k", name="r", permissions=["read"]))
        assert user.has_permission(Permission.READ)
        assert not user.has_permission(Permission.CREATE)


class TestRBACEndpoints:
    def test_public_path_no_key(self):
        client, _ = _make_client()
        assert client.get("/health").status_code == 200

    def test_protected_requires_key(self):
        client, _ = _make_client()
        assert client.get("/items").status_code == 401

    def test_invalid_key_rejected(self):
        client, _ = _make_client()
        r = client.get("/items", headers={"X-API-Key": "nope"})
        assert r.status_code == 401

    def test_valid_read_key_allowed(self):
        client, _ = _make_client()
        r = client.get("/items", headers={"X-API-Key": "reader-key"})
        assert r.status_code == 200

    def test_insufficient_permission_forbidden(self):
        client, _ = _make_client()
        r = client.post("/items", headers={"X-API-Key": "reader-key"})
        assert r.status_code == 403

    def test_all_permission_allowed(self):
        client, _ = _make_client()
        r = client.post("/items", headers={"X-API-Key": "admin-key"})
        assert r.status_code == 200

    def test_disabled_auth_allows_everything(self):
        client, _ = _make_client(enabled=False)
        assert client.get("/items").status_code == 200
        assert client.post("/items").status_code == 200


class TestPublicPathBoundary:
    def test_matches_segment_boundary_only(self):
        manager = AuthManager(_auth_config())
        manager.public_paths = ["/health", "/docs", "/openapi.json"]
        assert manager._is_public_path("/health")
        assert manager._is_public_path("/docs/oauth2-redirect")
        assert manager._is_public_path("/openapi.json")
        assert not manager._is_public_path("/healthz")
        assert not manager._is_public_path("/docsx")
        assert not manager._is_public_path("/openapi.json.bak")
        assert not manager._is_public_path("/api/v1/users")

    def test_trailing_slash_and_root(self):
        manager = AuthManager(_auth_config())
        manager.public_paths = ["/status/"]
        assert manager._is_public_path("/status")
        assert manager._is_public_path("/status/x")
        assert not manager._is_public_path("/statusx")
        manager.public_paths = ["/"]
        assert manager._is_public_path("/anything")

    def test_prefix_lookalike_requires_key(self):
        client, _ = _make_client()
        # "/health" is public; "/healthz"-style lookalikes are not.
        assert client.get("/health").status_code == 200
        assert client.get("/items").status_code == 401
