"""Generic CRUD operations for database tables."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

from warp.application.ports.database import DatabaseGateway
from warp.domain.errors import ValidationError
from warp.domain.pagination import PaginatedResponse, PaginationParams, paginate_response
from warp.domain.row_policy import EMPTY_POLICY, RowPolicy
from warp.domain.schema import TableSchema


class CRUDOperations:
    """Generic CRUD operations for any table.

    Provides a reusable interface for Create, Read, Update, Delete operations
    that works with any database adapter and table schema.
    """

    def __init__(
        self,
        db: DatabaseGateway,
        table_schema: TableSchema,
        response_model: type[BaseModel] | None = None,
        readonly_columns: list[str] | None = None,
        row_policy: RowPolicy | None = None,
    ):
        """Initialize CRUD operations.

        Args:
            db: Database adapter instance.
            table_schema: Schema of the table.
            response_model: Optional Pydantic model for response serialization.
            readonly_columns: Column names clients may never write
                (mass-assignment protection). The primary key and
                auto-generated columns are always protected in addition to these.
            row_policy: Mandatory row conditions for the calling key. Reads
                with a WHERE clause get them ANDed in; reads and writes by
                primary key are matched against them in memory, because there
                is no WHERE to push them into.
        """
        self.db = db
        self.schema = table_schema
        self.table_name = table_schema.table_name
        self.pk_column = table_schema.pk_column or "id"
        self.response_model = response_model
        self.row_policy = row_policy or EMPTY_POLICY

        # Mass-assignment protection: compute the columns a client is allowed
        # to write. Insertable columns already exclude auto-generated PK/serial/
        # identity columns; we further drop configured read-only columns, and
        # the PK is additionally immutable on update.
        self._readonly_columns = set(readonly_columns or [])
        self._creatable_columns = (
            set(table_schema.get_insertable_columns()) - self._readonly_columns
        )
        self._updatable_columns = self._creatable_columns - {self.pk_column}

    def with_policy(self, row_policy: RowPolicy) -> CRUDOperations:
        """A view of these operations bound to one caller's row policy.

        The schema-derived work (writable columns, primary key) is shared; only
        the policy differs. Binding per request rather than caching a policy on
        the instance is deliberate — a cached instance carrying one tenant's
        rules is exactly the bug this feature exists to prevent.
        """
        if row_policy is self.row_policy:
            return self
        bound = copy.copy(self)
        bound.row_policy = row_policy
        return bound

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
            filters=self.row_policy.apply(self.table_name, filters),
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

        A row outside the caller's row policy is reported as missing rather
        than refused: saying "this exists but is not yours" would itself leak
        that the record exists.

        Returns:
            Record dictionary, or None when it does not exist or the policy
            does not permit it.
        """
        extra = self._policy_only_columns(columns)
        record = await self.db.select_by_id(
            table=self.table_name,
            id_column=self.pk_column,
            id_value=id_value,
            columns=[*columns, *extra] if columns is not None and extra else columns,
        )
        if record is None or not self.row_policy.permits(self.table_name, record):
            return None
        # Drop only what was added for the check; anything else the adapter
        # returned is the caller's own projection to keep.
        return {k: v for k, v in record.items() if k not in extra} if extra else record

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
        self._reject_outside_policy(clean_data, "create")

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

        # Check before mutating: the row must already be one this caller may
        # touch, and the update must not move it out of their scope.
        if not await self._policy_allows_row(id_value):
            return None
        self._reject_outside_policy(clean_data, "update", partial=True)

        return await self.db.update(
            table=self.table_name, id_column=self.pk_column, id_value=id_value, data=clean_data
        )

    async def delete(self, id_value: Any) -> bool:
        """Delete a record by its primary key.

        Args:
            id_value: Primary key value.

        Returns:
            True if deleted, False when it does not exist or the caller's row
            policy does not cover it.
        """
        if not await self._policy_allows_row(id_value):
            return False
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
            table=self.table_name,
            filters=self.row_policy.apply(self.table_name, filters),
            pagination={"limit": 1, "offset": 0},
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

    # --- row policy ---------------------------------------------------------

    def _policy_only_columns(self, columns: list[str] | None) -> list[str]:
        """Columns the policy needs that the caller did not ask for.

        Selecting only ``id, name`` must not blind a check that reads
        ``tenant_id``, so those columns are fetched too — and dropped again
        before the row is returned, so the projection the caller asked for is
        what they get.
        """
        if columns is None:
            return []
        return sorted(self.row_policy.columns_for(self.table_name) - set(columns))

    async def _policy_allows_row(self, id_value: Any) -> bool:
        """Whether the addressed row exists and the policy covers it."""
        if not self.row_policy.covers(self.table_name):
            return True
        record = await self.db.select_by_id(
            table=self.table_name, id_column=self.pk_column, id_value=id_value
        )
        return self.row_policy.permits(self.table_name, record)

    def _reject_outside_policy(
        self, data: dict[str, Any], action: str, partial: bool = False
    ) -> None:
        """Refuse a write that would put the row outside the caller's scope.

        Without this a caller restricted to one tenant could create — or move
        a row into — another one, which is the row policy read backwards.

        Raises:
            ValidationError: When the written values contradict a rule.
        """
        conditions = self.row_policy.conditions_for(self.table_name)
        if not conditions:
            return
        for column, _operator, _value in conditions:
            if partial and column not in data:
                continue
            probe = dict(data)
            probe.setdefault(column, None)
            if not self.row_policy.permits(self.table_name, probe):
                raise ValidationError(
                    f"Cannot {action} a row outside your access scope (column '{column}')",
                    details={"column": column},
                )

    def _is_nullable(self, column_name: str) -> bool:
        """Check if a column is nullable."""
        col = self.schema.get_column(column_name)
        return col.nullable if col else True
