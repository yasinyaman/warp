"""End-to-end router tests: CRUD endpoints over a mock adapter via TestClient.

Exercises router_factory + crud + filtering/sorting/pagination together, and
verifies validation behavior (404s, invalid fields, mass-assignment 400s).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from warp.adapters.inbound.http.auth import AuthManager
from warp.adapters.inbound.http.governance import Governance
from warp.adapters.inbound.http.request_context import add_request_id_middleware
from warp.adapters.inbound.http.routes.crud import RouterFactory
from warp.application.config import ApiKeyConfig, AuthConfig
from warp.application.services.schema_discovery import SchemaAnalyzer
from warp.domain.masking import CatalogMasking, MaskingPolicy
from warp.domain.schema import ColumnSchema, TableSchema

SCHEMA = TableSchema(
    table_name="users",
    columns=[
        ColumnSchema(name="id", type="integer", nullable=False, extra="auto_increment"),
        ColumnSchema(name="username", type="varchar", nullable=False),
        ColumnSchema(name="email", type="varchar", nullable=False),
        ColumnSchema(name="status", type="varchar", nullable=True, default="active"),
        ColumnSchema(name="created_at", type="timestamp", nullable=True, default="now()"),
    ],
    primary_key="id",
)


@pytest.fixture
def client(mock_db):
    mock_db.add_mock_table(
        "users",
        {"table_name": "users", "primary_key": "id"},
        [
            {"id": 1, "username": "alice", "email": "alice@x.com", "status": "active"},
            {"id": 2, "username": "bob", "email": "bob@x.com", "status": "inactive"},
        ],
    )
    factory = RouterFactory(
        db=mock_db,
        schema_analyzer=SchemaAnalyzer(mock_db),
        readonly_columns=["created_at", "updated_at"],
    )
    app = FastAPI()
    app.include_router(factory.create_router(SCHEMA), prefix="/api/v1")
    return TestClient(app)


class TestRead:
    def test_list(self, client):
        r = client.get("/api/v1/users")
        assert r.status_code == 200
        assert r.json()["total"] == 2

    def test_list_pagination(self, client):
        r = client.get("/api/v1/users?limit=1&offset=0")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

    def test_list_with_sort_shortcut(self, client):
        assert client.get("/api/v1/users?sort=-id").status_code == 200

    def test_list_with_filter(self, client):
        assert client.get("/api/v1/users?filter[status]=active").status_code == 200

    def test_list_rejects_unknown_field(self, client):
        r = client.get("/api/v1/users?fields=id,does_not_exist")
        assert r.status_code == 400

    def test_get_by_id(self, client):
        r = client.get("/api/v1/users/1")
        assert r.status_code == 200
        assert r.json()["username"] == "alice"

    def test_get_missing_404(self, client):
        assert client.get("/api/v1/users/999").status_code == 404


class TestInjectionThroughApi:
    def test_malicious_filter_value_is_harmless(self, client):
        # The value is parameterized, so a SQL payload in the value is inert.
        r = client.get("/api/v1/users?filter[status]=1;DROP TABLE users;--")
        assert r.status_code == 200

    def test_limit_over_max_rejected(self, client):
        # Query bound le=max_limit (default 1000) -> 422 from FastAPI validation.
        assert client.get("/api/v1/users?limit=99999").status_code == 422


class TestWrite:
    def test_create(self, client):
        r = client.post("/api/v1/users", json={"username": "carol", "email": "c@x.com"})
        assert r.status_code == 201
        assert r.json()["username"] == "carol"

    def test_create_rejects_readonly_column(self, client):
        r = client.post(
            "/api/v1/users",
            json={"username": "x", "email": "x@x.com", "created_at": "2020-01-01T00:00:00"},
        )
        assert r.status_code == 400

    def test_create_rejects_auto_generated_and_unknown_fields(self, client):
        # Not silently dropped: the request model forbids fields it does not declare.
        r = client.post("/api/v1/users", json={"id": 5, "username": "x", "email": "e@x"})
        assert r.status_code == 422
        r = client.post("/api/v1/users", json={"username": "x", "email": "e@x", "bogus": 1})
        assert r.status_code == 422
        r = client.put("/api/v1/users/1", json={"nope": 1})
        assert r.status_code == 422

    def test_update(self, client):
        r = client.put("/api/v1/users/1", json={"status": "archived"})
        assert r.status_code == 200
        assert r.json()["status"] == "archived"

    def test_update_rejects_pk_change(self, client):
        r = client.put("/api/v1/users/1", json={"id": 999, "status": "x"})
        assert r.status_code == 400

    def test_update_missing_404(self, client):
        assert client.put("/api/v1/users/999", json={"status": "x"}).status_code == 404

    def test_delete(self, client):
        assert client.delete("/api/v1/users/1").status_code == 204
        assert client.get("/api/v1/users/1").status_code == 404

    def test_delete_missing_404(self, client):
        assert client.delete("/api/v1/users/999").status_code == 404


@pytest.fixture
def big_limit_client(mock_db):
    mock_db.add_mock_table("users", {"table_name": "users", "primary_key": "id"}, [])
    factory = RouterFactory(db=mock_db, schema_analyzer=SchemaAnalyzer(mock_db), max_limit=5000)
    app = FastAPI()
    app.include_router(factory.create_router(SCHEMA), prefix="/api/v1")
    return TestClient(app)


class TestInputValidation:
    """Bad client input is a 4xx, never a 500 from an unhandled ValueError."""

    def test_non_numeric_id_is_422(self, client):
        for method in ("get", "put", "patch", "delete"):
            kwargs = {"json": {"username": "x"}} if method in ("put", "patch") else {}
            r = getattr(client, method)("/api/v1/users/abc", **kwargs)
            assert r.status_code == 422, method
            assert "expected an integer" in r.json()["detail"]

    def test_get_rejects_unknown_fields(self, client):
        r = client.get("/api/v1/users/1?fields=id,nope")
        assert r.status_code == 400
        assert "nope" in r.json()["detail"]
        assert client.get("/api/v1/users/1?fields=id,username").status_code == 200

    def test_filter_value_of_wrong_kind_is_400(self, client):
        r = client.get("/api/v1/users?filter[id]=abc")
        assert r.status_code == 400
        assert "must be an integer" in r.json()["detail"]

    def test_unknown_filter_column_is_400(self, client):
        assert client.get("/api/v1/users?filter[nope]=1").status_code == 400

    def test_bad_sort_is_400(self, client):
        assert client.get("/api/v1/users?sort=id:sideways").status_code == 400
        assert client.get("/api/v1/users?sort=nope:asc").status_code == 400

    def test_text_filter_values_are_not_coerced(self, client, mock_db):
        # "007" on a varchar column must reach the adapter as text.
        captured = {}

        original = mock_db.select

        async def spy(*args, **kwargs):
            captured.update(kwargs)
            return await original(*args, **kwargs)

        mock_db.select = spy
        assert client.get("/api/v1/users?filter[username]=007").status_code == 200
        assert captured["filters"] == [("username", "eq", "007")]

    def test_configured_max_limit_above_1000(self, big_limit_client):
        assert big_limit_client.get("/api/v1/users?limit=2000").status_code == 200
        assert big_limit_client.get("/api/v1/users?limit=6000").status_code == 422


TENANT_SCHEMA = TableSchema(
    table_name="orders",
    columns=[
        ColumnSchema(name="id", type="integer", nullable=False, extra="auto_increment"),
        ColumnSchema(name="tenant_id", type="varchar", nullable=False),
        ColumnSchema(name="note", type="varchar", nullable=True),
    ],
    primary_key="id",
)


@pytest.fixture
def tenant_client(mock_db):
    """A CRUD app behind auth, with one key scoped to tenant 'acme'."""
    mock_db.add_mock_table(
        "orders",
        {"table_name": "orders", "primary_key": "id"},
        [
            {"id": 1, "tenant_id": "acme", "note": "ours"},
            {"id": 2, "tenant_id": "other", "note": "theirs"},
        ],
    )
    auth = AuthManager(
        AuthConfig(
            enabled=True,
            api_keys=[
                ApiKeyConfig(
                    key="acme-key",
                    name="acme",
                    permissions=["all"],
                    tenant="acme",
                    row_filters={"orders": [{"column": "tenant_id", "value": "${tenant}"}]},
                ),
                ApiKeyConfig(key="root-key", name="root", permissions=["all"]),
            ],
        )
    )
    factory = RouterFactory(db=mock_db, schema_analyzer=SchemaAnalyzer(mock_db), auth_manager=auth)
    app = FastAPI()
    app.include_router(factory.create_router(TENANT_SCHEMA), prefix="/api/v1")
    return TestClient(app)


ACME = {"X-API-Key": "acme-key"}
ROOT = {"X-API-Key": "root-key"}


class TestRowSecurityOverHttp:
    """The rules have to survive the whole request, not just the service call."""

    def test_the_list_is_scoped_to_the_callers_tenant(self, tenant_client):
        body = tenant_client.get("/api/v1/orders", headers=ACME).json()
        assert body["total"] == 1
        assert [item["id"] for item in body["items"]] == [1]

    def test_an_unrestricted_key_sees_every_row(self, tenant_client):
        assert tenant_client.get("/api/v1/orders", headers=ROOT).json()["total"] == 2

    def test_another_tenants_row_is_a_404(self, tenant_client):
        assert tenant_client.get("/api/v1/orders/2", headers=ACME).status_code == 404
        assert tenant_client.get("/api/v1/orders/1", headers=ACME).status_code == 200
        assert tenant_client.get("/api/v1/orders/2", headers=ROOT).status_code == 200

    def test_a_query_filter_cannot_reach_across_tenants(self, tenant_client):
        body = tenant_client.get("/api/v1/orders?filter[tenant_id]=other", headers=ACME).json()
        assert body["total"] == 0

    def test_writing_to_another_tenants_row_is_a_404(self, tenant_client):
        assert (
            tenant_client.patch(
                "/api/v1/orders/2", json={"note": "hijacked"}, headers=ACME
            ).status_code
            == 404
        )
        assert tenant_client.delete("/api/v1/orders/2", headers=ACME).status_code == 404

    def test_creating_into_another_tenant_is_refused(self, tenant_client):
        response = tenant_client.post(
            "/api/v1/orders", json={"tenant_id": "other", "note": "smuggled"}, headers=ACME
        )
        assert response.status_code == 400
        assert "access scope" in response.text

    def test_creating_into_your_own_tenant_works(self, tenant_client):
        response = tenant_client.post(
            "/api/v1/orders", json={"tenant_id": "acme", "note": "mine"}, headers=ACME
        )
        assert response.status_code == 201

    def test_one_callers_policy_does_not_leak_into_the_next_request(self, tenant_client):
        # The CRUD instance is shared across requests; the policy must not be.
        assert tenant_client.get("/api/v1/orders", headers=ACME).json()["total"] == 1
        assert tenant_client.get("/api/v1/orders", headers=ROOT).json()["total"] == 2
        assert tenant_client.get("/api/v1/orders", headers=ACME).json()["total"] == 1


PII_SCHEMA = TableSchema(
    table_name="people",
    columns=[
        ColumnSchema(name="id", type="integer", nullable=False, extra="auto_increment"),
        ColumnSchema(name="email", type="varchar", nullable=False),
        ColumnSchema(name="name", type="varchar", nullable=True),
    ],
    primary_key="id",
)

MASKING = CatalogMasking(
    policy=MaskingPolicy(
        rules={"email": "partial"},
        by_role={"support": {"email": "redact"}},
        exempt_roles=("admin",),
    ),
    semantic_types={"people": {"email": "email", "id": "id"}},
)


@pytest.fixture
def masked_client(mock_db):
    mock_db.add_mock_table(
        "people",
        {"table_name": "people", "primary_key": "id"},
        [{"id": 1, "email": "alice@example.com", "name": "Alice"}],
    )
    auth = AuthManager(
        AuthConfig(
            enabled=True,
            api_keys=[
                ApiKeyConfig(key="plain-key", name="plain", permissions=["all"]),
                ApiKeyConfig(
                    key="support-key", name="support", permissions=["all"], roles=["support"]
                ),
                ApiKeyConfig(key="admin-key", name="admin", permissions=["all"], roles=["admin"]),
            ],
        )
    )
    factory = RouterFactory(
        db=mock_db,
        schema_analyzer=SchemaAnalyzer(mock_db),
        auth_manager=auth,
        governance=Governance(masking=MASKING),
    )
    app = FastAPI()
    app.include_router(factory.create_router(PII_SCHEMA), prefix="/api/v1")
    return TestClient(app)


class TestMaskingOverHttp:
    def test_the_list_is_masked(self, masked_client):
        item = masked_client.get("/api/v1/people", headers={"X-API-Key": "plain-key"}).json()[
            "items"
        ][0]
        assert item["email"] == "a***@example.com"
        # Only the labelled column changes.
        assert item["name"] == "Alice"

    def test_a_single_record_is_masked(self, masked_client):
        body = masked_client.get("/api/v1/people/1", headers={"X-API-Key": "plain-key"}).json()
        assert body["email"] == "a***@example.com"

    def test_a_role_override_applies(self, masked_client):
        body = masked_client.get("/api/v1/people/1", headers={"X-API-Key": "support-key"}).json()
        assert body["email"] == "***"

    def test_an_exempt_role_sees_the_real_value(self, masked_client):
        body = masked_client.get("/api/v1/people/1", headers={"X-API-Key": "admin-key"}).json()
        assert body["email"] == "alice@example.com"

    def test_masking_does_not_break_response_validation(self, masked_client):
        # A text mask on a text column keeps the field's type.
        assert (
            masked_client.get("/api/v1/people/1", headers={"X-API-Key": "plain-key"}).status_code
            == 200
        )

    def test_one_callers_roles_do_not_leak_into_the_next_request(self, masked_client):
        assert (
            masked_client.get("/api/v1/people/1", headers={"X-API-Key": "admin-key"}).json()[
                "email"
            ]
            == "alice@example.com"
        )
        assert (
            masked_client.get("/api/v1/people/1", headers={"X-API-Key": "plain-key"}).json()[
                "email"
            ]
            == "a***@example.com"
        )


class RecordingSink:
    """Collects events so a test can assert what was recorded."""

    def __init__(self) -> None:
        self.events: list = []

    def record(self, event) -> None:
        self.events.append(event)


@pytest.fixture
def audited(mock_db):
    """CRUD behind auth, with a tenant-scoped key, masking and an audit sink."""
    mock_db.add_mock_table(
        "people",
        {"table_name": "people", "primary_key": "id"},
        [
            {"id": 1, "email": "alice@example.com", "name": "Alice"},
            {"id": 2, "email": "bob@example.com", "name": "Bob"},
        ],
    )
    sink = RecordingSink()
    auth = AuthManager(
        AuthConfig(
            enabled=True,
            api_keys=[
                ApiKeyConfig(
                    key="scoped-key",
                    name="scoped",
                    permissions=["all"],
                    tenant="acme",
                    row_filters={"people": [{"column": "name", "value": "Alice"}]},
                ),
                # `admin` is exempt from masking, so this key really is
                # unrestricted: no row filter and no masked column.
                ApiKeyConfig(key="root-key", name="root", permissions=["all"], roles=["admin"]),
            ],
        )
    )
    factory = RouterFactory(
        db=mock_db,
        schema_analyzer=SchemaAnalyzer(mock_db),
        auth_manager=auth,
        db_name="shop",
        governance=Governance(masking=MASKING, audit=sink),
    )
    app = FastAPI()
    add_request_id_middleware(app)
    app.include_router(factory.create_router(PII_SCHEMA), prefix="/api/v1")
    return TestClient(app), sink


class TestAuditTrail:
    def test_a_read_is_recorded_with_who_and_what(self, audited):
        client, sink = audited
        client.get("/api/v1/people", headers={"X-API-Key": "root-key"})
        event = sink.events[-1]
        assert event.action == "read"
        assert event.database == "shop"
        assert event.table == "people"
        assert event.actor == "root"
        assert event.row_count == 2
        assert event.status == 200

    def test_it_records_that_the_caller_was_restricted(self, audited):
        # The difference between "read the table" and "read their slice of it,
        # with a column masked" is the whole reason to keep this log.
        client, sink = audited
        client.get("/api/v1/people", headers={"X-API-Key": "scoped-key"})
        event = sink.events[-1]
        assert event.row_filtered is True
        assert event.masked_columns == ("email",)
        assert event.restricted is True
        assert event.tenant == "acme"
        assert event.row_count == 1

    def test_an_unrestricted_caller_is_recorded_as_unrestricted(self, audited):
        client, sink = audited
        client.get("/api/v1/people", headers={"X-API-Key": "root-key"})
        assert sink.events[-1].row_filtered is False
        assert sink.events[-1].restricted is False

    def test_filter_columns_are_recorded_but_never_their_values(self, audited):
        client, sink = audited
        client.get("/api/v1/people?filter[name]=Alice", headers={"X-API-Key": "root-key"})
        event = sink.events[-1]
        assert "name" in event.filtered_columns
        assert "Alice" not in str(event.as_dict())

    def test_a_probe_for_a_row_the_caller_cannot_see_is_recorded(self, audited):
        client, sink = audited
        client.get("/api/v1/people/2", headers={"X-API-Key": "scoped-key"})
        event = sink.events[-1]
        assert event.status == 404
        assert event.row_count == 0
        assert event.actor == "scoped"

    def test_writes_are_recorded(self, audited):
        client, sink = audited
        client.post(
            "/api/v1/people",
            json={"email": "carol@example.com", "name": "Carol"},
            headers={"X-API-Key": "root-key"},
        )
        created = sink.events[-1]
        assert created.action == "create" and created.is_write and created.status == 201

        client.delete("/api/v1/people/1", headers={"X-API-Key": "root-key"})
        deleted = sink.events[-1]
        assert deleted.action == "delete" and deleted.status == 204

    def test_a_refused_write_is_recorded_too(self, audited):
        client, sink = audited
        client.post(
            "/api/v1/people",
            json={"email": "x@example.com", "name": "Bob"},
            headers={"X-API-Key": "scoped-key"},
        )
        assert sink.events[-1].status == 400
        assert sink.events[-1].action == "create"

    def test_every_event_carries_the_requests_id(self, audited):
        client, sink = audited
        response = client.get("/api/v1/people", headers={"X-API-Key": "root-key"})
        assert response.headers["X-Request-ID"]
        assert sink.events[-1].request_id == response.headers["X-Request-ID"]

    def test_a_caller_supplied_request_id_is_honoured(self, audited):
        client, sink = audited
        client.get(
            "/api/v1/people",
            headers={"X-API-Key": "root-key", "X-Request-ID": "trace-abc-123"},
        )
        assert sink.events[-1].request_id == "trace-abc-123"

    def test_a_hostile_request_id_is_replaced(self, audited):
        # It is echoed in a header and written to the log, so it must not
        # carry arbitrary caller-controlled text.
        client, sink = audited
        client.get(
            "/api/v1/people",
            headers={"X-API-Key": "root-key", "X-Request-ID": "a b\nInjected: yes"},
        )
        assert sink.events[-1].request_id != "a b\nInjected: yes"
        assert sink.events[-1].request_id.isalnum()


HASH_KEY = b"an-export-key-of-sufficient-length!!"

HASHED = CatalogMasking(
    policy=MaskingPolicy(rules={"email": "hash"}, hash_key=HASH_KEY),
    semantic_types={"people": {"email": "email", "id": "id"}},
)


@pytest.fixture
def hashing_client(mock_db):
    """Two rows sharing an address, so correlation is observable."""
    mock_db.add_mock_table(
        "people",
        {"table_name": "people", "primary_key": "id"},
        [
            {"id": 1, "email": "alice@example.com", "name": "Alice"},
            {"id": 2, "email": "alice@example.com", "name": "Alice again"},
            {"id": 3, "email": "bob@example.com", "name": "Bob"},
        ],
    )
    factory = RouterFactory(
        db=mock_db,
        schema_analyzer=SchemaAnalyzer(mock_db),
        governance=Governance(masking=HASHED),
    )
    app = FastAPI()
    app.include_router(factory.create_router(PII_SCHEMA), prefix="/api/v1")
    return TestClient(app)


class TestHashMaskingOverHttp:
    """The key has to reach `mask_value` through the route, which is only
    exercised here — the domain tests call it directly."""

    def test_the_same_value_masks_the_same_way_in_two_rows(self, hashing_client):
        items = hashing_client.get("/api/v1/people").json()["items"]
        by_id = {row["id"]: row["email"] for row in items}

        assert by_id[1] == by_id[2], "the point of `hash` is that it correlates"
        assert by_id[1] != by_id[3]
        assert "alice" not in by_id[1]

    def test_the_digest_is_the_keyed_one(self, hashing_client):
        """Pins the route to the same derivation the domain tests pin."""
        from warp.domain.masking import mask_value

        body = hashing_client.get("/api/v1/people/1").json()
        assert body["email"] == mask_value("alice@example.com", "hash", HASH_KEY)

    def test_a_route_without_the_key_would_refuse_rather_than_leak(self, mock_db):
        """If the key ever stopped being threaded, this is what happens.

        Loudly, not silently — the failure mode a masking layer must not have
        is returning the raw value while reporting that it masked it.
        """
        from warp.domain.masking import MaskingError, mask_row

        with pytest.raises(MaskingError, match="hash_secret"):
            mask_row({"email": "alice@example.com"}, {"email": "hash"})


class TestMaskingStartupRefusesAnUnkeyedHash:
    """`_masking_for` is the composition seam, and the refusal belongs there.

    Checked before the catalog is loaded: a `hash` rule with no key is a
    misconfiguration whether or not this particular database happens to have
    an approved catalog, and an operator should hear about it either way.
    """

    def _settings(self, **masking):
        from warp.application.config import Settings

        settings = Settings().settings
        settings.masking.enabled = True
        for name, value in masking.items():
            setattr(settings.masking, name, value)
        return settings

    def test_a_hash_rule_without_a_secret_refuses(self):
        from warp.adapters.inbound.http.app import _masking_for

        with pytest.raises(ValueError, match="hash_secret"):
            _masking_for(None, self._settings(rules={"email": "hash"}), "db", PII_SCHEMA)

    def test_it_refuses_even_with_no_catalog_to_load(self):
        """The container is never touched, so `None` here is the assertion."""
        from warp.adapters.inbound.http.app import _masking_for

        with pytest.raises(ValueError, match="hash_secret"):
            _masking_for(
                None,
                self._settings(by_role={"support": {"phone": "hash"}}),
                "db",
                PII_SCHEMA,
            )

    def test_other_strategies_start_normally(self):
        from warp.adapters.inbound.http.app import _masking_for

        settings = self._settings(rules={"email": "partial"})
        # No catalog, so this returns NO_MASKING rather than raising.
        assert _masking_for(_NoCatalog(), settings, "db", PII_SCHEMA).is_empty


class _NoCatalog:
    """A container whose repository has nothing to give."""

    class repository:  # noqa: N801 - mimics the container's attribute shape
        @staticmethod
        def load(_name):
            raise FileNotFoundError("no catalog")
