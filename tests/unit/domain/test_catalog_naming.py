"""Tests for naming rules (moved from the file-store tests)."""

import pytest

from warp.domain.catalog_naming import names_are_similar, normalize_name, validate_catalog_name
from warp.domain.errors import InvalidCatalogNameError


@pytest.mark.parametrize(
    "input_name,expected",
    [
        ("users", "user"),
        ("tbl_users", "user"),
        ("user_id", "user"),
        ("categories", "category"),
        ("addresses", "address"),
        ("order_items", "order_item"),
    ],
)
def test_normalize_name(input_name, expected):
    assert normalize_name(input_name) == expected


@pytest.mark.parametrize(
    "name1,name2,expected",
    [
        ("user", "user", True),
        ("user", "users", True),
        ("abc", "abcdef", True),
        ("ab", "xy", False),
    ],
)
def test_names_are_similar(name1, name2, expected):
    assert names_are_similar(name1, name2) == expected


def test_validate_catalog_name():
    assert validate_catalog_name("primary_db") == "primary_db"
    with pytest.raises(InvalidCatalogNameError):
        validate_catalog_name("../x")
