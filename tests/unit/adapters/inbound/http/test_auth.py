"""Tests for API-key authentication, RBAC, and public paths."""

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from warp.adapters.inbound.http.auth import (
    AuthenticatedUser,
    AuthManager,
    Permission,
    caller_of,
)
from warp.application.config import ApiKeyConfig, AuthConfig


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


def _identity_client(enabled=True):
    """An app whose routes report the caller recorded for the request."""
    manager = AuthManager(_auth_config(enabled))
    app = FastAPI()

    @app.get("/health")
    async def health(request: Request):
        caller = caller_of(request)
        return {"caller": caller.name if caller else None}

    @app.get("/items", dependencies=[Depends(manager.require(Permission.READ))])
    async def list_items(request: Request):
        caller = caller_of(request)
        return {"caller": caller.name if caller else None}

    @app.delete(
        "/items/{item_id}",
        dependencies=[Depends(manager.require_any([Permission.DELETE, Permission.ALL]))],
    )
    async def delete_item(item_id: int, request: Request):
        caller = caller_of(request)
        return {"caller": caller.name if caller else None}

    return TestClient(app)


class TestCallerIdentityReachesHandlers:
    """Routes register auth as `dependencies=[...]`, whose return FastAPI drops.

    Without recording the caller on the request, nothing downstream could know
    who was asking — which is what row filtering and masking need.
    """

    def test_the_caller_is_available_to_the_handler(self):
        client = _identity_client()
        response = client.get("/items", headers={"X-API-Key": "reader-key"})
        assert response.status_code == 200
        assert response.json() == {"caller": "reader"}

    def test_require_any_records_the_caller_too(self):
        client = _identity_client()
        response = client.delete("/items/1", headers={"X-API-Key": "admin-key"})
        assert response.status_code == 200
        assert response.json() == {"caller": "admin"}

    def test_an_unauthenticated_request_is_unknown_not_permitted(self):
        # Auth off: the handler must see None and decide for itself, rather
        # than being handed something that looks like a permitted user.
        client = _identity_client(enabled=False)
        assert client.get("/items").json() == {"caller": None}

    def test_a_public_path_has_no_caller(self):
        client = _identity_client()
        assert client.get("/health", headers={"X-API-Key": "reader-key"}).json() == {"caller": None}

    def test_a_rejected_request_never_reaches_the_handler(self):
        client = _identity_client()
        assert client.get("/items").status_code == 401
        assert client.get("/items", headers={"X-API-Key": "nope"}).status_code == 401

    def test_a_caller_without_the_permission_is_refused(self):
        client = _identity_client()
        assert client.delete("/items/1", headers={"X-API-Key": "reader-key"}).status_code == 403

    def test_caller_of_on_a_request_that_never_passed_through_auth(self):
        # getattr-with-default, so an unauthenticated path cannot raise.
        assert caller_of(SimpleNamespace(state=SimpleNamespace())) is None


class TestRowRulesAndRawSql:
    """Raw SQL cannot have row conditions pushed into it, so it is denied."""

    def _user(self, **overrides) -> AuthenticatedUser:
        config = ApiKeyConfig(
            key="k",
            name="acme-reader",
            permissions=["all"],
            tenant="acme",
            row_filters={"orders": [{"column": "tenant_id", "value": "${tenant}"}]},
            **overrides,
        )
        return AuthenticatedUser(config)

    def test_a_restricted_caller_cannot_run_raw_sql_even_with_all(self):
        user = self._user()
        assert user.has_row_rules
        assert user.has_permission(Permission.READ)
        # `all` would otherwise grant it; one SELECT would make the rules moot.
        assert not user.has_permission(Permission.QUERY)

    def test_an_unrestricted_caller_still_can(self):
        user = AuthenticatedUser(ApiKeyConfig(key="k", name="admin", permissions=["all"]))
        assert not user.has_row_rules
        assert user.has_permission(Permission.QUERY)

    def test_the_policy_is_built_from_the_keys_tenant(self):
        assert self._user().row_policy.conditions_for("orders") == [("tenant_id", "eq", "acme")]

    def test_roles_and_tenant_are_carried(self):
        user = self._user(roles=["analyst"])
        assert user.tenant == "acme"
        assert user.roles == ["analyst"]

    def test_the_plaintext_key_is_still_not_retained(self):
        assert "k" not in vars(self._user()).values()
