"""Pagination utilities for API responses."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Pagination parameters extracted from query string.

    Usage:
        @app.get("/items")
        async def list_items(
            limit: int = Query(50, ge=1, le=1000),
            offset: int = Query(0, ge=0)
        ):
            pagination = PaginationParams(limit=limit, offset=offset)
    """

    limit: int = Field(default=50, ge=1, le=1000, description="Number of records to return")
    offset: int = Field(default=0, ge=0, description="Number of records to skip")

    @property
    def page(self) -> int:
        """Calculate current page number (1-indexed)."""
        if self.limit == 0:
            return 1
        return (self.offset // self.limit) + 1

    def to_dict(self) -> dict[str, int]:
        """Convert to dictionary for database adapter."""
        return {"limit": self.limit, "offset": self.offset}

    @classmethod
    def from_page(cls, page: int, page_size: int) -> "PaginationParams":
        """Create pagination params from page number and size.

        Args:
            page: Page number (1-indexed).
            page_size: Number of items per page.

        Returns:
            PaginationParams instance.
        """
        page = max(1, page)
        return cls(limit=page_size, offset=(page - 1) * page_size)


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response wrapper.

    Contains:
    - items: List of records
    - total: Total count without pagination
    - limit: Page size
    - offset: Current offset
    - page: Current page number
    - pages: Total number of pages
    """

    items: list[Any] = Field(default_factory=list)
    total: int = Field(default=0, description="Total number of records")
    limit: int = Field(default=50, description="Records per page")
    offset: int = Field(default=0, description="Current offset")

    @property
    def page(self) -> int:
        """Current page number (1-indexed)."""
        if self.limit == 0:
            return 1
        return (self.offset // self.limit) + 1

    @property
    def pages(self) -> int:
        """Total number of pages."""
        if self.limit == 0:
            return 1
        return (self.total + self.limit - 1) // self.limit

    @property
    def has_next(self) -> bool:
        """Check if there is a next page."""
        return self.offset + self.limit < self.total

    @property
    def has_prev(self) -> bool:
        """Check if there is a previous page."""
        return self.offset > 0

    model_config = ConfigDict(from_attributes=True)


def paginate_response(
    items: list[Any], total: int, pagination: PaginationParams
) -> PaginatedResponse[Any]:
    """Create a paginated response from items.

    Args:
        items: List of items for current page.
        total: Total count of items.
        pagination: Pagination parameters used.

    Returns:
        PaginatedResponse instance.
    """
    return PaginatedResponse(
        items=items, total=total, limit=pagination.limit, offset=pagination.offset
    )


def create_pagination_links(
    base_url: str, pagination: PaginationParams, total: int
) -> dict[str, str | None]:
    """Create pagination links for HATEOAS.

    Args:
        base_url: Base URL for the endpoint.
        pagination: Current pagination params.
        total: Total number of items.

    Returns:
        Dictionary with first, prev, next, last links.
    """
    links = {
        "first": f"{base_url}?limit={pagination.limit}&offset=0",
        "prev": None,
        "next": None,
        "last": None,
    }

    # Calculate last page offset
    if pagination.limit > 0:
        last_offset = ((total - 1) // pagination.limit) * pagination.limit
        last_offset = max(0, last_offset)
        links["last"] = f"{base_url}?limit={pagination.limit}&offset={last_offset}"

    # Previous page
    if pagination.offset > 0:
        prev_offset = max(0, pagination.offset - pagination.limit)
        links["prev"] = f"{base_url}?limit={pagination.limit}&offset={prev_offset}"

    # Next page
    if pagination.offset + pagination.limit < total:
        next_offset = pagination.offset + pagination.limit
        links["next"] = f"{base_url}?limit={pagination.limit}&offset={next_offset}"

    return links
