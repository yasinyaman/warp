"""Tests for column masking."""

import pytest

from warp.domain.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    TableCatalogEntry,
)
from warp.domain.masking import (
    NO_MASKING,
    REDACTED,
    CatalogMasking,
    MaskingError,
    MaskingPolicy,
    mask_row,
    mask_rows,
    mask_value,
    semantic_types_of,
    unsafe_rules,
    validate_strategy,
)

#: A fixed key, so the pinned digest below means something.
KEY = b"benchmark-key-for-tests-32-chars!"


class TestStrategies:
    def test_redact_keeps_nothing(self):
        assert mask_value("alice@example.com", "redact") == REDACTED

    def test_partial_keeps_the_email_domain(self):
        # "which provider" is usually what a support agent needs; the local
        # part is the identifying half.
        assert mask_value("alice@example.com", "partial") == "a***@example.com"

    def test_partial_on_a_plain_string_keeps_the_ends(self):
        assert mask_value("Alice Smith", "partial") == "A***h"

    def test_partial_refuses_to_leak_a_short_string(self):
        assert mask_value("ab", "partial") == REDACTED
        assert mask_value("a", "partial") == REDACTED

    def test_last4(self):
        assert mask_value("4111111111111234", "last4") == f"{REDACTED}1234"

    def test_last4_of_something_too_short_keeps_nothing(self):
        assert mask_value("123", "last4") == REDACTED

    def test_hash_is_stable_and_not_the_value(self):
        once = mask_value("alice@example.com", "hash", KEY)
        assert once == mask_value("alice@example.com", "hash", KEY)
        assert once != mask_value("bob@example.com", "hash", KEY)
        assert "alice" not in once

    def test_hash_is_stable_across_runs_for_a_given_key(self):
        """Pinned to a literal, which is the only assertion that can catch a
        change to the derivation.

        Its absence is why the strategy shipped as a bare truncated SHA-256:
        every other assertion here passes just as well for a per-process random
        key, which would silently break correlation with anything exported
        earlier — the one thing `hash` exists to provide.
        """
        assert mask_value("alice@example.com", "hash", KEY) == ("4b39c1f763170b98f8b84c70e852e6bb")

    def test_hash_without_a_key_refuses(self):
        with pytest.raises(MaskingError, match="hash_secret"):
            mask_value("alice@example.com", "hash")

    def test_hash_differs_under_a_different_key(self):
        """Rotating the key changes every pseudonym, which is what a key means."""
        assert mask_value("alice@example.com", "hash", KEY) != mask_value(
            "alice@example.com", "hash", b"a-different-key-of-sufficient-length"
        )

    def test_hash_is_not_a_bare_digest_of_the_value(self):
        """Regression guard: the defect was an unsalted SHA-256, truncated.

        An email falls to a wordlist and an 11-digit national id to a loop, so
        the unkeyed form was reversible however wide the digest.
        """
        import hashlib

        value = "alice@example.com"
        bare = hashlib.sha256(value.encode()).hexdigest()
        masked = mask_value(value, "hash", KEY)
        assert masked != bare[:12]
        assert masked not in bare
        # 128 bits: at 48, ten million addresses collide better than 1 in 6.
        assert len(masked) == 32

    def test_null(self):
        assert mask_value("anything", "null") is None

    @pytest.mark.parametrize("strategy", ["redact", "partial", "last4", "hash", "null"])
    def test_a_missing_value_stays_missing(self, strategy):
        # Masking a NULL would invent the appearance of data. `hash` needs no
        # key here: the None check comes first, deliberately.
        assert mask_value(None, strategy) is None

    def test_a_non_string_is_stringified_before_masking(self):
        assert mask_value(1234567, "last4") == f"{REDACTED}4567"

    def test_an_unknown_strategy_raises(self):
        with pytest.raises(MaskingError, match="Unknown masking strategy"):
            mask_value("x", "scramble")

    def test_validate_strategy_lists_the_real_ones(self):
        assert validate_strategy("redact", "email") == "redact"
        with pytest.raises(MaskingError, match="partial"):
            validate_strategy("redakt", "email")


class TestPolicy:
    def policy(self) -> MaskingPolicy:
        return MaskingPolicy(
            rules={"email": "partial", "phone": "last4"},
            by_role={"support": {"email": "redact"}},
            exempt_roles=("admin",),
        )

    def test_the_default_rules_apply_with_no_roles(self):
        assert self.policy().for_roles([]) == {"email": "partial", "phone": "last4"}

    def test_a_role_override_replaces_the_defaults_entirely(self):
        assert self.policy().for_roles(["support"]) == {"email": "redact"}

    def test_an_exempt_role_sees_raw_values(self):
        assert self.policy().for_roles(["admin"]) == {}

    def test_exemption_wins_over_an_override(self):
        assert self.policy().for_roles(["support", "admin"]) == {}

    def test_an_unknown_role_falls_back_to_the_defaults(self):
        assert self.policy().for_roles(["intern"]) == {"email": "partial", "phone": "last4"}

    def test_disabled_masks_nothing(self):
        disabled = MaskingPolicy(rules={"email": "redact"}, enabled=False)
        assert disabled.is_empty
        assert disabled.for_roles([]) == {}

    def test_columns_are_resolved_through_the_semantic_labels(self):
        labels = {"user_email": "email", "tel": "phone", "id": "id"}
        assert self.policy().columns_for(labels) == {"user_email": "partial", "tel": "last4"}

    def test_a_semantic_type_with_no_rule_is_untouched(self):
        assert self.policy().columns_for({"id": "id"}) == {}


class TestRowMasking:
    MASKS = {"email": "partial", "ssn": "redact"}

    def test_only_the_masked_columns_change(self):
        row = {"id": 1, "email": "a@x.com", "ssn": "123-45-6789", "name": "Alice"}
        assert mask_row(row, self.MASKS) == {
            "id": 1,
            "email": "a***@x.com",
            "ssn": REDACTED,
            "name": "Alice",
        }

    def test_the_original_row_is_not_mutated(self):
        row = {"email": "a@x.com"}
        mask_row(row, self.MASKS)
        assert row == {"email": "a@x.com"}

    def test_no_masks_is_a_plain_copy(self):
        row = {"email": "a@x.com"}
        assert mask_row(row, {}) == row

    def test_a_mask_for_a_column_the_row_lacks_is_harmless(self):
        assert mask_row({"id": 1}, self.MASKS) == {"id": 1}

    def test_batches(self):
        rows = [{"email": "a@x.com"}, {"email": "b@x.com"}]
        assert mask_rows(rows, self.MASKS) == [{"email": "a***@x.com"}, {"email": "b***@x.com"}]


class TestUnsafeRules:
    def test_a_text_mask_on_a_number_is_reported(self):
        problems = unsafe_rules({"amount": "partial"}, {"amount": ("float", True)})
        assert problems and "would replace it with text" in problems[0]

    def test_a_null_mask_on_a_not_null_column_is_reported(self):
        problems = unsafe_rules({"note": "null"}, {"note": ("str", False)})
        assert problems and "NOT NULL" in problems[0]

    def test_a_null_mask_on_a_nullable_column_is_fine(self):
        assert unsafe_rules({"note": "null"}, {"note": ("str", True)}) == []

    def test_a_text_mask_on_text_is_fine(self):
        assert unsafe_rules({"email": "redact"}, {"email": ("str", False)}) == []

    def test_an_unknown_column_is_skipped(self):
        assert unsafe_rules({"gone": "redact"}, {}) == []


class TestCatalogLabels:
    def catalog(self, status: CatalogStatus) -> DatabaseCatalog:
        return DatabaseCatalog(
            database_name="shop",
            database_type="postgresql",
            status=status,
            tables={
                "users": TableCatalogEntry(
                    table_name="users",
                    columns=[
                        ColumnCatalogEntry(name="id", data_type="integer", semantic_type="id"),
                        ColumnCatalogEntry(
                            name="email", data_type="varchar", semantic_type="email"
                        ),
                        ColumnCatalogEntry(name="note", data_type="varchar"),
                    ],
                )
            },
        )

    def test_an_approved_catalog_yields_its_labels(self):
        labels = semantic_types_of(self.catalog(CatalogStatus.approved))
        # The unlabelled column is simply absent.
        assert labels == {"users": {"id": "id", "email": "email"}}

    def test_a_draft_is_ignored(self):
        # Draft labels have not been reviewed; masking the wrong columns is as
        # damaging as masking none.
        assert semantic_types_of(self.catalog(CatalogStatus.draft)) == {}

    def test_no_catalog_at_all(self):
        assert semantic_types_of(None) == {}


class TestCatalogMasking:
    def masking(self) -> CatalogMasking:
        return CatalogMasking(
            policy=MaskingPolicy(rules={"email": "redact"}, exempt_roles=("admin",)),
            semantic_types={"users": {"email": "email", "id": "id"}},
        )

    def test_masks_resolve_per_table(self):
        assert self.masking().masks_for("users") == {"email": "redact"}
        assert self.masking().masks_for("orders") == {}

    def test_an_exempt_role_gets_nothing(self):
        assert self.masking().masks_for("users", ["admin"]) == {}

    def test_nothing_configured_masks_nothing(self):
        assert NO_MASKING.is_empty
        assert NO_MASKING.masks_for("users") == {}

    def test_rules_without_a_catalog_mask_nothing(self):
        # Nothing to key them on, which is why the app logs loudly instead.
        unlabelled = CatalogMasking(policy=MaskingPolicy(rules={"email": "redact"}))
        assert unlabelled.is_empty
        assert unlabelled.masks_for("users") == {}
