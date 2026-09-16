"""Generic CRUD operations for database tables."""

from typing import Any

from pydantic import BaseModel

from ..core.exceptions import ValidationError
from ..database.base import DatabaseAdapter
from ..schema.models import TableSchema
from ..utils.pagination import PaginatedResponse, PaginationParams, paginate_response


class CRUDOperations:
    """Generic CRUD operations for any table.

    Provides a reusable interface for Create, Read, Update, Delete operations
    that works with any database adapter and table schema.
    """

    def __init__(
        self,
        db: DatabaseAdapter,
        table_schema: TableSchema,
        response_model: type[BaseModel] | None = None,
        readonly_columns: list[str] | None = None,
    ):
        """Initialize CRUD operations.

        Args:
            db: Database adapter instance.
            table_schema: Schema of the table.
            response_model: Optional Pydantic model for response serialization.
            readonly_columns: Column names clients may never write
                (mass-assignment protection). The primary key and
                auto-generated columns are always protected in addition to these.
        """
        self.db = db
        self.schema = table_schema
        self.table_name = table_schema.table_name
        self.pk_column = table_schema.pk_column or "id"
        self.response_model = response_model

        # Mass-assignment protection: compute the columns a client is allowed
        # to write. Insertable columns already exclude auto-generated PK/serial/
        # identity columns; we further drop configured read-only columns, and
        # the PK is additionally immutable on update.
        self._readonly_columns = set(readonly_columns or [])
        self._creatable_columns = (
            set(table_schema.get_insertable_columns()) - self._readonly_columns
        )
        self._updatable_columns = self._creatable_columns - {self.pk_column}

    async def get_all(
        self,
        columns: list[str] | None = None,
        filters: list[tuple[str, str, Any]] | None = None,
        pagination: PaginationParams | None = None,
        sort: list[tuple[str, str]] | None = None,
    ) -> PaginatedResponse[Any]:
        """Get all records with filtering, pagination, and sorting.

        Args:
            columns: Optional list of columns to select.
            filters: Optional list of (column, operator, value) filter tuples.
            pagination: Optional pagination parameters.
            sort: Optional list of (column, direction) sort tuples.

        Returns:
            PaginatedResponse containing items and metadata.
        """
        pagination = pagination or PaginationParams()

        items, total = await self.db.select(
            table=self.table_name,
            columns=columns,
            filters=filters,
            pagination=pagination.to_dict(),
            sort=sort,
        )

        return paginate_response(items, total, pagination)

    async def get_by_id(
        self, id_value: Any, columns: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Get a single record by its primary key.

        Args:
            id_value: Primary key value.
            columns: Optional list of columns to select.

        Returns:
            Record dictionary or None if not found.
        """
        return await self.db.select_by_id(
            table=self.table_name, id_column=self.pk_column, id_value=id_value, columns=columns
        )

    async def create(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create a new record.

        Args:
            data: Dictionary of column-value pairs.

        Returns:
            Created record with generated values.
        """
        self._reject_non_writable(data, self._creatable_columns, "set")

        # Filter out None values if column is not nullable without default
        clean_data = {k: v for k, v in data.items() if v is not None or self._is_nullable(k)}

        return await self.db.insert(table=self.table_name, data=clean_data)

    async def update(self, id_value: Any, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update an existing record.

        Args:
            id_value: Primary key value.
            data: Dictionary of column-value pairs to update.

        Returns:
            Updated record or None if not found.
        """
        self._reject_non_writable(data, self._updatable_columns, "update")

        # Filter out None values for partial updates
        clean_data = {k: v for k, v in data.items() if v is not None}

        if not clean_data:
            # No fields to update, just return existing record
            return await self.get_by_id(id_value)

        return await self.db.update(
            table=self.table_name, id_column=self.pk_column, id_value=id_value, data=clean_data
        )

    async def delete(self, id_value: Any) -> bool:
        """Delete a record by its primary key.

        Args:
            id_value: Primary key value.

        Returns:
            True if deleted, False if not found.
        """
        return await self.db.delete(
            table=self.table_name, id_column=self.pk_column, id_value=id_value
        )

    async def exists(self, id_value: Any) -> bool:
        """Check if a record exists.

        Args:
            id_value: Primary key value.

        Returns:
            True if record exists.
        """
        record = await self.get_by_id(id_value, columns=[self.pk_column])
        return record is not None

    async def count(self, filters: list[tuple[str, str, Any]] | None = None) -> int:
        """Count records matching filters.

        Args:
            filters: Optional list of filter tuples.

        Returns:
            Number of matching records.
        """
        _, total = await self.db.select(
            table=self.table_name, filters=filters, pagination={"limit": 1, "offset": 0}
        )
        return total

    def _reject_non_writable(self, data: dict[str, Any], allowed: set[str], action: str) -> None:
        """Reject attempts to write read-only, auto-generated, or unknown columns.

        Raises:
            ValidationError: If ``data`` contains any column not in ``allowed``.
        """
        offending = set(data) - allowed
        if offending:
            raise ValidationError(
                f"Cannot {action} read-only or unknown column(s): {', '.join(sorted(offending))}",
                details={
                    "columns": sorted(offending),
                    "writable": sorted(allowed),
                },
            )

    def _is_nullable(self, column_name: str) -> bool:
        """Check if a column is nullable."""
        col = self.schema.get_column(column_name)
        return col.nullable if col else True
