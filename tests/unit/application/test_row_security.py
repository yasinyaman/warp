"""Row-level security end to end through CRUDOperations.

These are the tests that matter: not that the rules parse, but that a caller
scoped to one tenant cannot reach another's rows by any route — list, by id,
update, delete or create.
"""

from typing import Any

import pytest

from warp.application.services.crud import CRUDOperations
from warp.domain.errors import ValidationError
from warp.domain.row_policy import build_policy
from warp.domain.schema import ColumnSchema, TableSchema

ACME = {"tenant": "acme", "username": "acme-reader"}
ROWS = [
    {"id": 1, "tenant_id": "acme", "name": "acme-one"},
    {"id": 2, "tenant_id": "other", "name": "other-one"},
    {"id": 3, "tenant_id": "acme", "name": "acme-two"},
]


class RecordingGateway:
    """Applies filters for real, so a forgotten condition shows up as a row."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = [dict(r) for r in rows]
        self.select_filters: list[Any] = []
        self.deleted: list[Any] = []
        self.updated: list[tuple[Any, dict[str, Any]]] = []

    def _matches(self, row: dict[str, Any], filters: Any) -> bool:
        for column, operator, value in filters or []:
            actual = row.get(column)
            if operator == "eq" and actual != value:
                return False
            if operator == "ne" and actual == value:
                return False
            if operator == "in" and actual not in value:
                return False
        return True

    async def select(self, table, columns=None, filters=None, pagination=None, sort=None):
        self.select_filters.append(filters)
        matching = [r for r in self.rows if self._matches(r, filters)]
        return matching, len(matching)

    async def select_by_id(self, table, key, columns=None):
        for row in self.rows:
            if all(row.get(column) == value for column, value in key.items()):
                return dict(row) if columns is None else {c: row[c] for c in columns if c in row}
        return None

    async def insert(self, table, data):
        created = {"id": max((r["id"] for r in self.rows), default=0) + 1, **data}
        self.rows.append(created)
        return created

    async def update(self, table, key, data):
        self.updated.append((key, data))
        for row in self.rows:
            if all(row.get(column) == value for column, value in key.items()):
                row.update(data)
                return dict(row)
        return None

    async def delete(self, table, key):
        self.deleted.append(key)
        before = len(self.rows)
        self.rows = [
            r for r in self.rows if not all(r.get(column) == value for column, value in key.items())
        ]
        return len(self.rows) != before


def schema() -> TableSchema:
    return TableSchema(
        table_name="orders",
        columns=[
            ColumnSchema(name="id", type="integer", nullable=False),
            ColumnSchema(name="tenant_id", type="varchar", nullable=False),
            ColumnSchema(name="name", type="varchar", nullable=True),
        ],
        primary_key="id",
    )


@pytest.fixture
def gateway() -> RecordingGateway:
    return RecordingGateway(ROWS)


@pytest.fixture
def scoped(gateway: RecordingGateway) -> CRUDOperations:
    policy = build_policy({"orders": [{"column": "tenant_id", "value": "${tenant}"}]}, caller=ACME)
    return CRUDOperations(db=gateway, table_schema=schema(), row_policy=policy)


@pytest.fixture
def unrestricted(gateway: RecordingGateway) -> CRUDOperations:
    return CRUDOperations(db=gateway, table_schema=schema())


class TestReads:
    async def test_list_only_returns_the_callers_rows(self, scoped):
        page = await scoped.get_all()
        assert [item["id"] for item in page.items] == [1, 3]
        assert page.total == 2

    async def test_the_condition_reaches_the_database_not_a_python_filter(self, scoped, gateway):
        await scoped.get_all()
        assert ("tenant_id", "eq", "acme") in gateway.select_filters[-1]

    async def test_a_caller_filter_cannot_widen_the_policy(self, scoped, gateway):
        # Asking for the other tenant explicitly still returns nothing.
        await scoped.get_all(filters=[("tenant_id", "eq", "other")])
        applied = gateway.select_filters[-1]
        assert applied[-1] == ("tenant_id", "eq", "acme")
        assert len(applied) == 2

    async def test_count_is_scoped_too(self, scoped):
        assert await scoped.count() == 2

    async def test_by_id_returns_the_callers_row(self, scoped):
        assert (await scoped.get_by_id({"id": 1}))["name"] == "acme-one"

    async def test_by_id_reports_another_tenants_row_as_missing(self, scoped):
        # Not 403: saying "exists but not yours" discloses that it exists.
        assert await scoped.get_by_id({"id": 2}) is None

    async def test_exists_follows_the_policy(self, scoped):
        assert await scoped.exists({"id": 1})
        assert not await scoped.exists({"id": 2})

    async def test_a_projection_cannot_hide_the_policy_column(self, scoped):
        # Selecting only id,name must not blind the tenant_id check...
        assert await scoped.get_by_id({"id": 2}, columns=["id", "name"]) is None
        # ...and the column added for the check is not returned.
        allowed = await scoped.get_by_id({"id": 1}, columns=["id", "name"])
        assert allowed == {"id": 1, "name": "acme-one"}

    async def test_an_unrestricted_caller_sees_everything(self, unrestricted):
        page = await unrestricted.get_all()
        assert [item["id"] for item in page.items] == [1, 2, 3]
        assert await unrestricted.get_by_id({"id": 2}) is not None


class TestWrites:
    async def test_update_of_another_tenants_row_is_a_miss_and_changes_nothing(
        self, scoped, gateway
    ):
        assert await scoped.update({"id": 2}, {"name": "hijacked"}) is None
        assert gateway.updated == []
        assert gateway.rows[1]["name"] == "other-one"

    async def test_update_of_your_own_row_works(self, scoped, gateway):
        updated = await scoped.update({"id": 1}, {"name": "renamed"})
        assert updated["name"] == "renamed"
        assert gateway.updated == [({"id": 1}, {"name": "renamed"})]

    async def test_delete_of_another_tenants_row_reports_not_found(self, scoped, gateway):
        assert await scoped.delete({"id": 2}) is False
        assert gateway.deleted == []
        assert len(gateway.rows) == 3

    async def test_delete_of_your_own_row_works(self, scoped, gateway):
        assert await scoped.delete({"id": 1}) is True
        assert [r["id"] for r in gateway.rows] == [2, 3]

    async def test_create_outside_the_scope_is_refused(self, scoped):
        # The row policy read backwards: without this a caller could create
        # rows into a tenant they cannot read.
        with pytest.raises(ValidationError, match="outside your access scope"):
            await scoped.create({"tenant_id": "other", "name": "smuggled"})

    async def test_create_without_the_scope_column_is_refused(self, scoped):
        # Omitting tenant_id would let the database default decide.
        with pytest.raises(ValidationError, match="outside your access scope"):
            await scoped.create({"name": "no tenant"})

    async def test_create_inside_the_scope_works(self, scoped, gateway):
        created = await scoped.create({"tenant_id": "acme", "name": "new"})
        assert created["tenant_id"] == "acme"
        assert len(gateway.rows) == 4

    async def test_moving_a_row_to_another_tenant_is_refused(self, scoped):
        with pytest.raises(ValidationError, match="outside your access scope"):
            await scoped.update({"id": 1}, {"tenant_id": "other"})

    async def test_an_update_that_does_not_touch_the_scope_column_is_fine(self, scoped):
        assert (await scoped.update({"id": 1}, {"name": "ok"}))["name"] == "ok"


class TestBinding:
    def test_with_policy_does_not_mutate_the_shared_instance(self, unrestricted):
        policy = build_policy({"orders": [{"column": "tenant_id", "value": "acme"}]})
        bound = unrestricted.with_policy(policy)
        assert bound is not unrestricted
        assert unrestricted.row_policy.is_empty
        assert not bound.row_policy.is_empty

    def test_binding_the_same_policy_reuses_the_instance(self, unrestricted):
        assert unrestricted.with_policy(unrestricted.row_policy) is unrestricted

    def test_the_bound_view_shares_the_schema_work(self, unrestricted):
        policy = build_policy({"orders": [{"column": "tenant_id", "value": "acme"}]})
        bound = unrestricted.with_policy(policy)
        assert bound.table_name == unrestricted.table_name
        assert bound._creatable_columns == unrestricted._creatable_columns
