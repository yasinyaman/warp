"""``FilterParser.parse_conditions``: structured (JSON body) filters."""

from datetime import datetime

import pytest

from warp.domain.filtering import FilterParser, parse_filter_conditions

KINDS = {"id": "int", "price": "float", "active": "bool", "name": "str", "created_at": "datetime"}
COLUMNS = list(KINDS)


def parse(conditions):
    return parse_filter_conditions(conditions, COLUMNS, KINDS)


def test_typed_json_values_pass_through():
    assert parse([("id", "eq", 5), ("price", "gt", 1.5), ("active", "eq", True)]) == [
        ("id", "eq", 5),
        ("price", "gt", 1.5),
        ("active", "eq", True),
    ]


def test_strings_are_coerced_by_kind():
    out = parse([("id", "eq", "5"), ("price", "gt", "1.5"), ("created_at", "gte", "2024-01-02")])
    assert out[0] == ("id", "eq", 5)
    assert out[1] == ("price", "gt", 1.5)
    assert out[2][2] == datetime(2024, 1, 2)


def test_in_requires_list_and_coerces_items():
    assert parse([("id", "in", ["1", 2, "3"])]) == [("id", "in", [1, 2, 3])]
    with pytest.raises(ValueError, match="needs a list"):
        parse([("id", "in", "1,2,3")])


def test_is_null_accepts_bool_and_strings():
    assert parse([("name", "is_null", True)]) == [("name", "is_null", True)]
    assert parse([("name", "is_null", "false")]) == [("name", "is_null", False)]
    assert parse([("name", "is_null", 0)]) == [("name", "is_null", False)]


def test_like_is_always_text():
    assert parse([("name", "like", 42)]) == [("name", "like", "42")]


def test_unknown_column_and_operator_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        parse([("nope", "eq", 1)])
    with pytest.raises(ValueError, match="Invalid operator"):
        parse([("id", "between", 1)])


def test_unconvertible_string_rejected():
    with pytest.raises(ValueError):
        parse([("id", "eq", "abc")])


def test_parser_returns_filter_conditions():
    conds = FilterParser(COLUMNS, KINDS).parse_conditions([("id", "ne", "7")])
    assert conds[0].column == "id" and conds[0].operator == "ne" and conds[0].value == 7


def test_no_kinds_guesses_strings_like_query_strings():
    assert parse_filter_conditions([("x", "eq", "5")]) == [("x", "eq", 5)]
    assert parse_filter_conditions([("x", "eq", "abc")]) == [("x", "eq", "abc")]
