"""
Raw SQL query endpoint.
"""
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..database.base import DatabaseAdapter
from .auth import AuthManager, Permission


class QueryRequest(BaseModel):
    """Request model for raw SQL query execution."""
    query: str = Field(..., description="SQL query to execute")
    params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Named parameters for the query (e.g., {'name': 'John'})"
    )


class QueryResponse(BaseModel):
    """Response model for query results."""
    success: bool = True
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    message: Optional[str] = None


class QueryValidator:
    """
    Validates SQL queries against whitelist.

    Security measure to prevent dangerous SQL operations.
    """

    def __init__(self, whitelist: Optional[List[str]] = None):
        """
        Initialize the validator.

        Args:
            whitelist: List of allowed SQL commands (e.g., ['SELECT', 'INSERT']).
                      If None, only SELECT is allowed.
        """
        self.whitelist = [cmd.upper() for cmd in (whitelist or ["SELECT"])]

    def validate(self, query: str) -> bool:
        """
        Validate a SQL query against the whitelist.

        Args:
            query: SQL query string.

        Returns:
            True if query is allowed.

        Raises:
            ValueError: If query uses a disallowed command.
        """
        # Normalize query
        normalized = query.strip().upper()

        # Check for dangerous patterns
        dangerous_patterns = [
            r";\s*(DROP|DELETE|TRUNCATE|ALTER|CREATE|GRANT|REVOKE)",
            r"--",  # SQL comments
            r"/\*",  # Multi-line comments
            r"EXEC\s*\(",
            r"EXECUTE\s+",
            r"xp_",  # SQL Server extended procedures
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, normalized):
                raise ValueError(
                    "Query contains potentially dangerous patterns"
                )

        # Check if query starts with allowed command
        for allowed in self.whitelist:
            if normalized.startswith(allowed):
                return True

        raise ValueError(
            f"Query must start with one of: {', '.join(self.whitelist)}"
        )


def create_query_router(
    db: DatabaseAdapter,
    whitelist: Optional[List[str]] = None,
    enabled: bool = True,
    auth_manager: Optional[AuthManager] = None
) -> APIRouter:
    """
    Create a router for raw SQL query execution.

    Args:
        db: Database adapter instance.
        whitelist: List of allowed SQL commands.
        enabled: Whether raw queries are enabled.
        auth_manager: Optional auth manager for permission control.

    Returns:
        FastAPI router with query endpoint.
    """
    router = APIRouter(prefix="/query", tags=["Raw Query"])
    validator = QueryValidator(whitelist)

    # Auth dependencies
    def get_auth_deps(permission: Permission) -> List:
        if auth_manager and auth_manager.enabled:
            return [Depends(auth_manager.require(permission))]
        return []

    if not enabled:
        @router.post("/execute", response_model=QueryResponse)
        async def execute_query_disabled(request: QueryRequest):
            """Raw query execution is disabled."""
            raise HTTPException(
                status_code=403,
                detail="Raw SQL query execution is disabled"
            )
        return router

    @router.post(
        "/execute",
        response_model=QueryResponse,
        summary="Execute Raw SQL Query",
        description="""
Execute a raw SQL query against the database.

**Security Notes:**
- Only whitelisted SQL commands are allowed
- Use parameterized queries to prevent SQL injection
- Query results are limited for safety

**Example:**
```json
{
    "query": "SELECT * FROM users WHERE status = :status LIMIT 10",
    "params": {"status": "active"}
}
```
        """,
        dependencies=get_auth_deps(Permission.QUERY)
    )
    async def execute_query(request: QueryRequest):
        """Execute a raw SQL query."""
        try:
            # Validate query
            validator.validate(request.query)

            # Execute query
            rows = await db.execute_query(
                query=request.query,
                params=request.params
            )

            return QueryResponse(
                success=True,
                rows=rows,
                row_count=len(rows)
            )

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Query execution failed: {str(e)}"
            )

    @router.get(
        "/allowed-commands",
        summary="Get Allowed SQL Commands",
        description="Returns the list of SQL commands that are allowed for raw queries."
    )
    async def get_allowed_commands():
        """Get list of allowed SQL commands."""
        return {
            "allowed_commands": validator.whitelist,
            "enabled": enabled
        }

    return router
