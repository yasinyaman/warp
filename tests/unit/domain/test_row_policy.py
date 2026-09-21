"""Tests for row-level security rules."""

from datetime import date
from decimal import Decimal

import pytest

from warp.domain.row_policy import (
    EMPTY_POLICY,
    PolicyError,
    RowPolicy,
    RowRule,
    build_policy,
)

ACME = {"tenant": "acme", "username": "acme-reader"}


class TestRuleConstruction:
    def test_a_rule_needs_a_column(self):
        with pytest.raises(PolicyError, match="needs a column"):
            RowRule(column="")

    def test_an_unknown_operator_lists_the_known_ones(self):
        with pytest.raises(PolicyError, match="between"):
            RowRule(column="a", operator="between")
        with pytest.raises(PolicyError, match="is_null"):
            RowRule(column="a", operator="between")

    @pytest.mark.parametrize("operator", ["eq", "ne", "gt", "gte", "lt", "lte", "like", "is_null"])
    def test_every_filter_operator_is_usable_as_a_rule(self, operator):
        assert RowRule(column="a", operator=operator).operator == operator


class TestTemplates:
    def test_tenant_is_substituted(self):
        rule = RowRule(column="tenant_id", value="${tenant}")
        assert rule.resolve(ACME) == ("tenant_id", "eq", "acme")

    def test_username_is_substituted(self):
        rule = RowRule(column="owner", value="${username}")
        assert rule.resolve(ACME) == ("owner", "eq", "acme-reader")

    def test_a_literal_value_passes_through(self):
        assert RowRule(column="active", value=True).resolve(ACME)[2] is True

    @pytest.mark.parametrize("caller", [{}, {"tenant": None}, {"tenant": ""}])
    def test_an_unresolvable_template_refuses_rather_than_widening(self, caller):
        # Substituting "" would quietly turn the rule into "rows whose tenant
        # is blank", which is not a restriction at all.
        with pytest.raises(PolicyError, match="not set on this API key"):
            RowRule(column="tenant_id", value="${tenant}").resolve(caller)

    def test_in_needs_a_list(self):
        with pytest.raises(PolicyError, match="needs a list"):
            RowRule(column="region", operator="in", value="eu").resolve(ACME)
        assert RowRule(column="region", operator="in", value=["eu", "us"]).resolve(ACME)[2] == [
            "eu",
            "us",
        ]


class TestMatching:
    def rule(self, **kwargs) -> RowRule:
        return RowRule(**kwargs)

    def test_equality(self):
        rule = self.rule(column="tenant_id", value="${tenant}")
        assert rule.permits({"tenant_id": "acme"}, ACME)
        assert not rule.permits({"tenant_id": "other"}, ACME)

    def test_a_missing_column_is_refused_not_ignored(self):
        # The check cannot be decided, so it must not pass.
        assert not self.rule(column="tenant_id", value="acme").permits({"id": 1}, ACME)

    def test_null_never_satisfies_a_comparison(self):
        assert not self.rule(column="tenant_id", value="acme").permits({"tenant_id": None}, ACME)

    def test_is_null(self):
        rule = self.rule(column="deleted_at", operator="is_null", value=True)
        assert rule.permits({"deleted_at": None}, ACME)
        assert not rule.permits({"deleted_at": "2024-01-01"}, ACME)

    def test_types_the_driver_returns_still_match(self):
        # A tenant column read back as Decimal must still match a configured 7.
        assert self.rule(column="t", value=7).permits({"t": Decimal("7")}, ACME)
        assert self.rule(column="t", value="7").permits({"t": 7}, ACME)
        assert not self.rule(column="t", value=7).permits({"t": 8}, ACME)

    def test_a_boolean_matches_zero_or_one(self):
        # MySQL returns tinyint(1) as bool, so a rule written `active = 1` must
        # still match the rows its SQL form would have selected. Matching the
        # SQL path matters more than Python's type purity: being stricter here
        # than in the WHERE clause turns a legitimate row into a 404.
        assert self.rule(column="flag", value=1).permits({"flag": True}, ACME)
        assert self.rule(column="flag", value=0).permits({"flag": False}, ACME)
        assert not self.rule(column="flag", value=1).permits({"flag": False}, ACME)

    def test_in(self):
        rule = self.rule(column="region", operator="in", value=["eu", "us"])
        assert rule.permits({"region": "eu"}, ACME)
        assert not rule.permits({"region": "apac"}, ACME)

    def test_like(self):
        rule = self.rule(column="path", operator="like", value="acme/%")
        assert rule.permits({"path": "acme/reports/1"}, ACME)
        assert not rule.permits({"path": "other/reports/1"}, ACME)

    def test_like_treats_the_pattern_as_sql_not_regex(self):
        rule = self.rule(column="code", operator="like", value="a.c")
        assert rule.permits({"code": "a.c"}, ACME)
        assert not rule.permits({"code": "abc"}, ACME)

    def test_ordering(self):
        rule = self.rule(column="created", operator="gte", value=date(2024, 1, 1))
        assert rule.permits({"created": date(2024, 6, 1)}, ACME)
        assert not rule.permits({"created": date(2023, 6, 1)}, ACME)

    def test_incomparable_values_are_refused(self):
        rule = self.rule(column="created", operator="gte", value=date(2024, 1, 1))
        assert not rule.permits({"created": "not a date"}, ACME)


class TestPolicy:
    def policy(self) -> RowPolicy:
        return build_policy(
            {"orders": [{"column": "tenant_id", "value": "${tenant}"}]}, caller=ACME
        )

    def test_conditions_are_filter_tuples(self):
        assert self.policy().conditions_for("orders") == [("tenant_id", "eq", "acme")]

    def test_a_table_with_no_rules_is_unrestricted(self):
        policy = self.policy()
        assert policy.conditions_for("products") == []
        assert not policy.covers("products")
        assert policy.permits("products", {"anything": 1})

    def test_policy_conditions_come_after_the_callers_own(self):
        # Last word: a caller's filters can only narrow further.
        applied = self.policy().apply("orders", [("status", "eq", "paid")])
        assert applied == [("status", "eq", "paid"), ("tenant_id", "eq", "acme")]

    def test_apply_with_no_caller_filters(self):
        assert self.policy().apply("orders", None) == [("tenant_id", "eq", "acme")]

    def test_columns_for_reports_what_a_projection_must_keep(self):
        assert self.policy().columns_for("orders") == {"tenant_id"}

    def test_all_rules_must_hold(self):
        policy = build_policy(
            {
                "orders": [
                    {"column": "tenant_id", "value": "${tenant}"},
                    {"column": "archived", "value": False},
                ]
            },
            caller=ACME,
        )
        assert policy.permits("orders", {"tenant_id": "acme", "archived": False})
        assert not policy.permits("orders", {"tenant_id": "acme", "archived": True})

    def test_a_missing_row_is_never_permitted(self):
        assert not self.policy().permits("orders", None)

    def test_the_empty_policy_restricts_nothing(self):
        assert EMPTY_POLICY.is_empty
        assert EMPTY_POLICY.apply("orders", None) == []
        assert EMPTY_POLICY.permits("orders", {"tenant_id": "whatever"})


class TestBuilding:
    def test_empty_config(self):
        assert build_policy(None).is_empty
        assert build_policy({}).is_empty
        assert build_policy({"orders": []}).is_empty

    def test_op_is_accepted_as_an_alias_for_operator(self):
        policy = build_policy({"t": [{"column": "a", "op": "gte", "value": 1}]})
        assert policy.conditions_for("t") == [("a", "gte", 1)]

    def test_a_malformed_rule_fails_loudly(self):
        # Silently dropping it would leave the table unrestricted.
        with pytest.raises(PolicyError, match="must be an object"):
            build_policy({"orders": ["tenant_id = acme"]})
        with pytest.raises(PolicyError, match="needs a column"):
            build_policy({"orders": [{"value": "acme"}]})

    def test_prebuilt_rules_pass_through(self):
        policy = build_policy({"orders": [RowRule(column="a", value=1)]})
        assert policy.conditions_for("orders") == [("a", "eq", 1)]
