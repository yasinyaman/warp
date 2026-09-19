"""Authentication and authorization module for API endpoints."""

import hashlib
import secrets
from collections.abc import Awaitable, Callable
from enum import StrEnum

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from warp.application.config import ApiKeyConfig, AuthConfig
from warp.domain.row_policy import EMPTY_POLICY, RowPolicy, build_policy


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
        """Store the identity, permissions and row policy from the API-key config."""
        self.name = api_key_config.name
        # Deliberately do NOT retain the plaintext API key on the user object.
        self.permissions = api_key_config.permissions
        self.tenant = api_key_config.tenant
        self.roles = list(api_key_config.roles)
        self.row_policy = build_policy(
            {
                table: [rule.model_dump() for rule in rules]
                for table, rules in api_key_config.row_filters.items()
            },
            caller={"tenant": api_key_config.tenant, "username": api_key_config.name},
        )

    @property
    def has_row_rules(self) -> bool:
        """Whether any row rule restricts what this caller may read or write."""
        return not self.row_policy.is_empty

    def has_permission(self, permission: Permission) -> bool:
        """Check if user has the required permission.

        ``query`` is the exception: raw SQL cannot have row conditions pushed
        into it, so a caller with row rules is never allowed to run it —
        otherwise the rules would be one ``SELECT`` away from irrelevant.
        """
        if permission is Permission.QUERY and self.has_row_rules:
            return False
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
        """Hash an API key for constant-length, timing-safe comparison.

        Unkeyed, unlike the masking module's `hash` strategy, and deliberately
        so: the input here is a generated token with ~256 bits of entropy, so
        there is no dictionary to attack. The masking case hashes emails and
        national ids, which are enumerable — that is the difference, not the
        algorithm.
        """
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

    @staticmethod
    def _remember(request: Request, user: AuthenticatedUser | None) -> AuthenticatedUser | None:
        """Record who is calling on the request, then hand the user back.

        Routes register auth as ``dependencies=[Depends(...)]``, and FastAPI
        throws away what such a dependency returns — so without this the
        identity built here would never reach a handler. Stashing it on
        ``request.state`` gives every route access to the caller without
        changing a single signature.

        ``None`` means "nobody was authenticated", which happens when auth is
        off or the path is public. Anything that filters or masks by identity
        must treat that as *unknown*, not as *permitted*.
        """
        request.state.user = user
        return user

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
                return self._remember(request, None)

            # Check if path is public
            if self._is_public_path(request.url.path):
                return self._remember(request, None)

            # Get current user
            user = await self.get_current_user(request, api_key)

            if user is None:
                # Auth disabled or public path
                return self._remember(request, None)

            # Check permission
            if not user.has_permission(permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: '{permission.value}' required",
                )

            return self._remember(request, user)

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
                return self._remember(request, None)

            if self._is_public_path(request.url.path):
                return self._remember(request, None)

            user = await self.get_current_user(request, api_key)

            if user is None:
                return self._remember(request, None)

            # Check if user has any of the required permissions
            for permission in permissions:
                if user.has_permission(permission):
                    return self._remember(request, user)

            perm_names = [p.value for p in permissions]
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: one of {perm_names} required",
            )

        return permission_checker


def roles_of(request: Request) -> list[str]:
    """The roles recorded for this request (empty when nobody is identified)."""
    caller = caller_of(request)
    return caller.roles if caller is not None else []


def policy_of(request: Request) -> RowPolicy:
    """The row policy recorded for this request.

    An unauthenticated request has no policy, which is why a configured row
    rule also requires authentication to be on (see
    ``validate_production_config``).
    """
    caller = caller_of(request)
    return caller.row_policy if caller is not None else EMPTY_POLICY


def caller_of(request: Request) -> AuthenticatedUser | None:
    """The authenticated caller recorded for this request, if any.

    ``None`` means nobody was authenticated — auth is disabled, or the path is
    public. It never means "permitted": a caller that filters or masks rows by
    identity has to decide what to do about an unknown one, and the safe
    answer is to refuse rather than to serve everything.
    """
    return getattr(request.state, "user", None)
