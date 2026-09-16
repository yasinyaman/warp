"""Authentication and authorization module for API endpoints."""

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from enum import StrEnum

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from ..config.settings import ApiKeyConfig, AuthConfig


class Permission(StrEnum):
    """Available permissions for API operations."""

    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    QUERY = "query"  # For raw SQL queries
    ALL = "all"


class AuthenticatedUser:
    """Represents an authenticated API user."""

    def __init__(self, api_key_config: ApiKeyConfig):
        """Store the identity and permissions from the API-key config."""
        self.name = api_key_config.name
        # Deliberately do NOT retain the plaintext API key on the user object.
        self.permissions = api_key_config.permissions

    def has_permission(self, permission: Permission) -> bool:
        """Check if user has the required permission."""
        if Permission.ALL.value in self.permissions:
            return True
        return permission.value in self.permissions


class AuthManager:
    """Manages API authentication and authorization.

    Usage:
        auth_manager = AuthManager(auth_config)

        # In router:
        @router.get("/items", dependencies=[Depends(auth_manager.require(Permission.READ))])
        async def list_items():
            ...
    """

    def __init__(self, auth_config: AuthConfig):
        """Index API keys by hash and set up the header scheme."""
        self.config = auth_config
        self.enabled = auth_config.enabled
        self.header_name = auth_config.header_name
        # Store sha256(key) -> config instead of plaintext keys. Empty/unset
        # keys are dropped so they can never authenticate.
        self._key_hashes: list[tuple[str, ApiKeyConfig]] = [
            (self._hash_key(key.key), key) for key in auth_config.api_keys if key.key
        ]
        self.api_key_count = len(self._key_hashes)
        self.public_paths = auth_config.public_paths

        # Create API key header scheme
        self.api_key_header = APIKeyHeader(
            name=self.header_name, auto_error=False, description="API Key for authentication"
        )

    @staticmethod
    def _hash_key(api_key: str) -> str:
        """Hash an API key for constant-length, timing-safe comparison."""
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def _get_api_key_config(self, api_key: str | None) -> ApiKeyConfig | None:
        """Look up the config for a presented key using a timing-safe comparison.

        Uses ``secrets.compare_digest`` against the stored hashes and scans all
        configured keys without short-circuiting, so neither the comparison nor
        the number of keys leaks timing information about the secret.
        """
        if not api_key:
            return None
        candidate = self._hash_key(api_key)
        match: ApiKeyConfig | None = None
        for stored_hash, config in self._key_hashes:
            if secrets.compare_digest(stored_hash, candidate):
                match = config
        return match

    def _is_public_path(self, path: str) -> bool:
        """Check if the path is public (no auth required).

        A public path matches itself and its sub-paths on a segment boundary
        (``/docs`` covers ``/docs/oauth2-redirect`` but not ``/docsx``).
        """
        for public_path in self.public_paths:
            base = public_path.rstrip("/")
            if not base:  # "/" makes everything public
                return True
            if path == base or path.startswith(base + "/"):
                return True
        return False

    async def get_current_user(
        self, request: Request, api_key: str | None = None
    ) -> AuthenticatedUser | None:
        """Get the current authenticated user from API key.

        Returns None if auth is disabled or path is public.
        Raises HTTPException if auth is required but invalid.
        """
        # If auth is disabled, allow all
        if not self.enabled:
            return None

        # Check if path is public
        if self._is_public_path(request.url.path):
            return None

        # Get API key from header
        if api_key is None:
            api_key = request.headers.get(self.header_name)

        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required",
                headers={self.header_name: "API key is missing"},
            )

        # Validate API key
        key_config = self._get_api_key_config(api_key)
        if key_config is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={self.header_name: "Invalid API key"},
            )

        return AuthenticatedUser(key_config)

    def require(self, permission: Permission) -> Callable[..., Awaitable[AuthenticatedUser | None]]:
        """Create a dependency that requires a specific permission.

        Usage:
            @router.get("/items", dependencies=[Depends(auth.require(Permission.READ))])
        """

        async def permission_checker(
            request: Request, api_key: str | None = Depends(self.api_key_header)
        ) -> AuthenticatedUser | None:
            # If auth is disabled, allow all
            if not self.enabled:
                return None

            # Check if path is public
            if self._is_public_path(request.url.path):
                return None

            # Get current user
            user = await self.get_current_user(request, api_key)

            if user is None:
                # Auth disabled or public path
                return None

            # Check permission
            if not user.has_permission(permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: '{permission.value}' required",
                )

            return user

        return permission_checker

    def require_any(
        self, permissions: list[Permission]
    ) -> Callable[..., Awaitable[AuthenticatedUser | None]]:
        """Create a dependency that requires any of the specified permissions.

        Usage:
            @router.put("/items/{id}", dependencies=[Depends(auth.require_any([Permission.UPDATE, Permission.ALL]))])
        """

        async def permission_checker(
            request: Request, api_key: str | None = Depends(self.api_key_header)
        ) -> AuthenticatedUser | None:
            if not self.enabled:
                return None

            if self._is_public_path(request.url.path):
                return None

            user = await self.get_current_user(request, api_key)

            if user is None:
                return None

            # Check if user has any of the required permissions
            for permission in permissions:
                if user.has_permission(permission):
                    return user

            perm_names = [p.value for p in permissions]
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: one of {perm_names} required",
            )

        return permission_checker


# Global auth manager instance (initialized from settings)
_auth_manager: AuthManager | None = None


def init_auth_manager(auth_config: AuthConfig) -> AuthManager:
    """Initialize the global auth manager."""
    global _auth_manager  # noqa: PLW0603
    _auth_manager = AuthManager(auth_config)
    return _auth_manager


def get_auth_manager() -> AuthManager | None:
    """Get the global auth manager instance."""
    return _auth_manager
