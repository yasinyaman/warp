"""
Tests for utility modules: pagination, filtering, sorting.
"""

import pytest

from warp.utils.filtering import (
    FilterCondition,
    FilterParser,
    parse_filters_from_request,
)
from warp.utils.pagination import (
    PaginatedResponse,
    PaginationParams,
    create_pagination_links,
    paginate_response,
)
from warp.utils.sorting import (
    SortField,
    SortParser,
    parse_sort_from_request,
)

# ===========================================
# Pagination Tests
# ===========================================


class TestPaginationParams:
    """Tests for PaginationParams."""

    def test_defaults(self):
        """Test default values."""
        params = PaginationParams()
        assert params.limit == 50
        assert params.offset == 0

    def test_custom_values(self):
        """Test custom values."""
        params = PaginationParams(limit=25, offset=100)
        assert params.limit == 25
        assert params.offset == 100

    def test_page_calculation(self):
        """Test page number calculation."""
        params = PaginationParams(limit=10, offset=0)
        assert params.page == 1

        params = PaginationParams(limit=10, offset=10)
        assert params.page == 2

        params = PaginationParams(limit=10, offset=25)
        assert params.page == 3

    def test_to_dict(self):
        """Test conversion to dictionary."""
        params = PaginationParams(limit=20, offset=40)
        result = params.to_dict()

        assert result == {"limit": 20, "offset": 40}

    def test_from_page(self):
        """Test creation from page number."""
        params = PaginationParams.from_page(page=1, page_size=10)
        assert params.limit == 10
        assert params.offset == 0

        params = PaginationParams.from_page(page=3, page_size=20)
        assert params.limit == 20
        assert params.offset == 40

    def test_from_page_handles_zero(self):
        """Test that page 0 is treated as page 1."""
        params = PaginationParams.from_page(page=0, page_size=10)
        assert params.offset == 0


class TestPaginatedResponse:
    """Tests for PaginatedResponse."""

    def test_basic_response(self):
        """Test basic response creation."""
        response = PaginatedResponse(items=[{"id": 1}, {"id": 2}], total=100, limit=10, offset=0)
        assert len(response.items) == 2
        assert response.total == 100
        assert response.page == 1
        assert response.pages == 10

    def test_has_next(self):
        """Test has_next property."""
        # Has next
        response = PaginatedResponse(total=100, limit=10, offset=0)
        assert response.has_next is True

        # No next (last page)
        response = PaginatedResponse(total=100, limit=10, offset=90)
        assert response.has_next is False

        # No next (exactly at end)
        response = PaginatedResponse(total=20, limit=10, offset=10)
        assert response.has_next is False

    def test_has_prev(self):
        """Test has_prev property."""
        # No prev (first page)
        response = PaginatedResponse(total=100, limit=10, offset=0)
        assert response.has_prev is False

        # Has prev
        response = PaginatedResponse(total=100, limit=10, offset=10)
        assert response.has_prev is True

    def test_pages_calculation(self):
        """Test total pages calculation."""
        # Exact division
        response = PaginatedResponse(total=100, limit=10)
        assert response.pages == 10

        # With remainder
        response = PaginatedResponse(total=95, limit=10)
        assert response.pages == 10

        # Single page
        response = PaginatedResponse(total=5, limit=10)
        assert response.pages == 1


class TestPaginateResponse:
    """Tests for paginate_response helper."""

    def test_creates_response(self):
        """Test response creation."""
        items = [{"id": 1}, {"id": 2}]
        pagination = PaginationParams(limit=10, offset=20)

        response = paginate_response(items, total=100, pagination=pagination)

        assert response.items == items
        assert response.total == 100
        assert response.limit == 10
        assert response.offset == 20


class TestCreatePaginationLinks:
    """Tests for HATEOAS pagination links."""

    def test_first_page_links(self):
        """Test links on first page."""
        pagination = PaginationParams(limit=10, offset=0)
        links = create_pagination_links("/api/items", pagination, total=100)

        assert links["first"] == "/api/items?limit=10&offset=0"
        assert links["prev"] is None
        assert links["next"] == "/api/items?limit=10&offset=10"
        assert links["last"] == "/api/items?limit=10&offset=90"

    def test_middle_page_links(self):
        """Test links on middle page."""
        pagination = PaginationParams(limit=10, offset=50)
        links = create_pagination_links("/api/items", pagination, total=100)

        assert links["prev"] == "/api/items?limit=10&offset=40"
        assert links["next"] == "/api/items?limit=10&offset=60"

    def test_last_page_links(self):
        """Test links on last page."""
        pagination = PaginationParams(limit=10, offset=90)
        links = create_pagination_links("/api/items", pagination, total=100)

        assert links["prev"] == "/api/items?limit=10&offset=80"
        assert links["next"] is None


# ===========================================
# Filtering Tests
# ===========================================


class TestFilterCondition:
    """Tests for FilterCondition."""

    def test_to_tuple(self):
        """Test conversion to tuple."""
        condition = FilterCondition(column="status", operator="eq", value="active")
        result = condition.to_tuple()

        assert result == ("status", "eq", "active")


class TestFilterParser:
    """Tests for FilterParser."""

    def test_simple_filter(self):
        """Test simple equality filter."""
        parser = FilterParser()
        params = {"filter[status]": "active"}

        filters = parser.parse(params)

        assert len(filters) == 1
        assert filters[0].column == "status"
        assert filters[0].operator == "eq"
        assert filters[0].value == "active"

    def test_filter_with_operator(self):
        """Test filter with explicit operator."""
        parser = FilterParser()
        params = {"filter[price][gte]": "100"}

        filters = parser.parse(params)

        assert filters[0].column == "price"
        assert filters[0].operator == "gte"
        assert filters[0].value == 100  # Converted to int

    def test_multiple_filters(self):
        """Test multiple filters."""
        parser = FilterParser()
        params = {
            "filter[status]": "active",
            "filter[price][gte]": "50",
            "filter[price][lte]": "200",
        }

        filters = parser.parse(params)

        assert len(filters) == 3

    def test_in_operator(self):
        """Test IN operator with comma-separated values."""
        parser = FilterParser()
        params = {"filter[status][in]": "active,pending,processing"}

        filters = parser.parse(params)

        assert filters[0].operator == "in"
        assert filters[0].value == ["active", "pending", "processing"]

    def test_is_null_operator(self):
        """Test IS NULL operator."""
        parser = FilterParser()

        # True
        params = {"filter[deleted_at][is_null]": "true"}
        filters = parser.parse(params)
        assert filters[0].value is True

        # False
        params = {"filter[deleted_at][is_null]": "false"}
        filters = parser.parse(params)
        assert filters[0].value is False

    def test_like_operator(self):
        """Test LIKE operator."""
        parser = FilterParser()
        params = {"filter[name][like]": "%john%"}

        filters = parser.parse(params)

        assert filters[0].operator == "like"
        assert filters[0].value == "%john%"

    def test_value_type_conversion(self):
        """Test automatic type conversion."""
        parser = FilterParser()

        # Integer
        params = {"filter[count]": "42"}
        filters = parser.parse(params)
        assert filters[0].value == 42
        assert isinstance(filters[0].value, int)

        # Float
        params = {"filter[price]": "19.99"}
        filters = parser.parse(params)
        assert filters[0].value == 19.99
        assert isinstance(filters[0].value, float)

        # Boolean
        params = {"filter[active]": "true"}
        filters = parser.parse(params)
        assert filters[0].value is True

    def test_allowed_columns(self):
        """Test column whitelist."""
        parser = FilterParser(allowed_columns=["status", "name"])

        # Allowed
        params = {"filter[status]": "active"}
        filters = parser.parse(params)
        assert len(filters) == 1

        # Not allowed
        params = {"filter[secret]": "value"}
        with pytest.raises(ValueError, match="not allowed"):
            parser.parse(params)

    def test_invalid_operator(self):
        """Test invalid operator raises error."""
        parser = FilterParser()
        params = {"filter[name][invalid_op]": "value"}

        with pytest.raises(ValueError, match="Invalid operator"):
            parser.parse(params)

    def test_ignores_non_filter_params(self):
        """Test that non-filter params are ignored."""
        parser = FilterParser()
        params = {"filter[status]": "active", "limit": "10", "sort": "name", "other": "value"}

        filters = parser.parse(params)

        assert len(filters) == 1


class TestParseFiltersFromRequest:
    """Tests for parse_filters_from_request helper."""

    def test_returns_tuples(self):
        """Test that it returns list of tuples."""
        params = {"filter[status]": "active"}
        result = parse_filters_from_request(params)

        assert result == [("status", "eq", "active")]


# ===========================================
# Sorting Tests
# ===========================================


class TestSortField:
    """Tests for SortField."""

    def test_default_direction(self):
        """Test default ascending direction."""
        field = SortField(column="name")
        assert field.direction == "asc"

    def test_explicit_direction(self):
        """Test explicit direction."""
        field = SortField(column="name", direction="desc")
        assert field.direction == "desc"

    def test_invalid_direction(self):
        """Test invalid direction raises error."""
        with pytest.raises(ValueError, match="Invalid sort direction"):
            SortField(column="name", direction="invalid")

    def test_direction_normalized(self):
        """Test direction is lowercased."""
        field = SortField(column="name", direction="DESC")
        assert field.direction == "desc"

    def test_to_tuple(self):
        """Test conversion to tuple."""
        field = SortField(column="name", direction="desc")
        assert field.to_tuple() == ("name", "desc")


class TestSortParser:
    """Tests for SortParser."""

    def test_single_field_default_asc(self):
        """Test single field with default ascending."""
        parser = SortParser()
        fields = parser.parse("name")

        assert len(fields) == 1
        assert fields[0].column == "name"
        assert fields[0].direction == "asc"

    def test_colon_notation(self):
        """Test column:direction notation."""
        parser = SortParser()
        fields = parser.parse("name:desc")

        assert fields[0].column == "name"
        assert fields[0].direction == "desc"

    def test_prefix_notation(self):
        """Test -column for descending."""
        parser = SortParser()

        # Descending
        fields = parser.parse("-created_at")
        assert fields[0].column == "created_at"
        assert fields[0].direction == "desc"

        # Ascending
        fields = parser.parse("+name")
        assert fields[0].column == "name"
        assert fields[0].direction == "asc"

    def test_multiple_fields(self):
        """Test multiple sort fields."""
        parser = SortParser()
        fields = parser.parse("status:asc,created_at:desc,name")

        assert len(fields) == 3
        assert fields[0].to_tuple() == ("status", "asc")
        assert fields[1].to_tuple() == ("created_at", "desc")
        assert fields[2].to_tuple() == ("name", "asc")

    def test_mixed_notation(self):
        """Test mixed prefix and colon notation."""
        parser = SortParser()
        fields = parser.parse("-created_at,name:asc,+id")

        assert fields[0].to_tuple() == ("created_at", "desc")
        assert fields[1].to_tuple() == ("name", "asc")
        assert fields[2].to_tuple() == ("id", "asc")

    def test_empty_string(self):
        """Test empty string returns empty list."""
        parser = SortParser()
        fields = parser.parse("")
        assert fields == []

    def test_allowed_columns(self):
        """Test column whitelist."""
        parser = SortParser(allowed_columns=["name", "created_at"])

        # Allowed
        fields = parser.parse("name:asc")
        assert len(fields) == 1

        # Not allowed
        with pytest.raises(ValueError, match="not allowed"):
            parser.parse("secret:asc")


class TestParseSortFromRequest:
    """Tests for parse_sort_from_request helper."""

    def test_returns_tuples(self):
        """Test that it returns list of tuples."""
        result = parse_sort_from_request("name:desc,id:asc")
        assert result == [("name", "desc"), ("id", "asc")]

    def test_default_sort(self):
        """Test default sort when none provided."""
        result = parse_sort_from_request(None, default_sort=[("created_at", "desc")])
        assert result == [("created_at", "desc")]

    def test_none_without_default(self):
        """Test None returns empty list without default."""
        result = parse_sort_from_request(None)
        assert result == []


class TestTypedFilterParsing:
    """With column kinds known, values are converted to the column's type, never guessed."""

    KINDS = {"zip": "str", "id": "int", "price": "float", "active": "bool", "name": "str"}

    def _parse(self, params):
        return FilterParser(column_kinds=self.KINDS).parse(params)

    def test_text_columns_keep_text(self):
        assert self._parse({"filter[zip]": "00123"})[0].value == "00123"
        assert self._parse({"filter[name]": "true"})[0].value == "true"
        assert self._parse({"filter[name]": "null"})[0].value == "null"
        assert self._parse({"filter[zip]": "1e5"})[0].value == "1e5"

    def test_int_column_converts_or_rejects(self):
        assert self._parse({"filter[id]": "42"})[0].value == 42
        with pytest.raises(ValueError, match="must be an integer"):
            self._parse({"filter[id]": "abc"})
        with pytest.raises(ValueError, match="must be an integer"):
            self._parse({"filter[id]": "1.5"})

    def test_float_column_converts_or_rejects(self):
        assert self._parse({"filter[price][gte]": "19.99"})[0].value == 19.99
        with pytest.raises(ValueError, match="must be a number"):
            self._parse({"filter[price]": "cheap"})

    def test_bool_column_accepts_common_spellings(self):
        assert self._parse({"filter[active]": "1"})[0].value is True
        assert self._parse({"filter[active]": "no"})[0].value is False
        with pytest.raises(ValueError, match="must be a boolean"):
            self._parse({"filter[active]": "maybe"})

    def test_in_operator_uses_column_kind(self):
        assert self._parse({"filter[id][in]": "1,2,3"})[0].value == [1, 2, 3]
        assert self._parse({"filter[zip][in]": "01,02"})[0].value == ["01", "02"]

    def test_like_pattern_is_always_text(self):
        assert self._parse({"filter[id][like]": "%1%"})[0].value == "%1%"

    def test_unknown_kind_falls_back_to_guessing(self):
        parser = FilterParser(column_kinds={"id": "int"})
        assert parser.parse({"filter[other]": "42"})[0].value == 42
        assert parser.parse({"filter[other]": "true"})[0].value is True

    def test_convenience_function_threads_kinds(self):
        result = parse_filters_from_request({"filter[zip]": "007"}, ["zip"], {"zip": "str"})
        assert result == [("zip", "eq", "007")]


class TestPaginationLimitIsNotCapped:
    def test_limit_above_legacy_cap_is_valid(self):
        # The configurable pagination.max_limit is enforced at the route, not here.
        assert PaginationParams(limit=5000).limit == 5000
        assert PaginationParams.from_page(page=2, page_size=2000).offset == 2000
