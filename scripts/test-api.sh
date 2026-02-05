#!/bin/bash
# ===========================================
# Auto CRUD API - Test Script
# ===========================================
# Usage: ./scripts/test-api.sh [base_url]

BASE_URL="${1:-http://localhost:8000}"
API_URL="${BASE_URL}/api/v1"

echo "==================================="
echo "Auto CRUD API - Test Script"
echo "Base URL: ${BASE_URL}"
echo "==================================="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counter
PASSED=0
FAILED=0

test_endpoint() {
    local method=$1
    local endpoint=$2
    local data=$3
    local expected_status=$4
    local description=$5

    if [ -n "$data" ]; then
        response=$(curl -s -w "\n%{http_code}" -X "$method" "${API_URL}${endpoint}" \
            -H "Content-Type: application/json" \
            -d "$data")
    else
        response=$(curl -s -w "\n%{http_code}" -X "$method" "${API_URL}${endpoint}")
    fi

    status_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | sed '$d')

    if [ "$status_code" == "$expected_status" ]; then
        echo -e "${GREEN}✓${NC} ${description} (${method} ${endpoint}) - Status: ${status_code}"
        ((PASSED++))
    else
        echo -e "${RED}✗${NC} ${description} (${method} ${endpoint})"
        echo "  Expected: ${expected_status}, Got: ${status_code}"
        echo "  Response: ${body}"
        ((FAILED++))
    fi
}

echo "--- Health Checks ---"
echo ""

# Health check
response=$(curl -s -w "\n%{http_code}" "${BASE_URL}/health")
status=$(echo "$response" | tail -n1)
if [ "$status" == "200" ]; then
    echo -e "${GREEN}✓${NC} Health check passed"
    ((PASSED++))
else
    echo -e "${RED}✗${NC} Health check failed"
    ((FAILED++))
fi

# Ready check
response=$(curl -s -w "\n%{http_code}" "${BASE_URL}/ready")
status=$(echo "$response" | tail -n1)
if [ "$status" == "200" ]; then
    echo -e "${GREEN}✓${NC} Readiness check passed"
    ((PASSED++))
else
    echo -e "${RED}✗${NC} Readiness check failed"
    ((FAILED++))
fi

# Info endpoint
response=$(curl -s -w "\n%{http_code}" "${BASE_URL}/info")
status=$(echo "$response" | tail -n1)
if [ "$status" == "200" ]; then
    echo -e "${GREEN}✓${NC} Info endpoint passed"
    ((PASSED++))
else
    echo -e "${RED}✗${NC} Info endpoint failed"
    ((FAILED++))
fi

echo ""
echo "--- CRUD Operations: Users ---"
echo ""

# List users
test_endpoint "GET" "/users" "" "200" "List users"

# List users with pagination
test_endpoint "GET" "/users?limit=2&offset=0" "" "200" "List users with pagination"

# List users with filtering
test_endpoint "GET" "/users?filter[status]=active" "" "200" "List users with filter"

# List users with sorting
test_endpoint "GET" "/users?sort=username:asc" "" "200" "List users with sorting"

# Get single user
test_endpoint "GET" "/users/1" "" "200" "Get user by ID"

# Get non-existent user
test_endpoint "GET" "/users/99999" "" "404" "Get non-existent user (404)"

echo ""
echo "--- CRUD Operations: Products ---"
echo ""

# List products
test_endpoint "GET" "/products" "" "200" "List products"

# Filter products by status
test_endpoint "GET" "/products?filter[status]=active" "" "200" "Filter products by status"

# Filter products by price range
test_endpoint "GET" "/products?filter[price][gte]=100&filter[price][lte]=1000" "" "200" "Filter products by price range"

# Sort products
test_endpoint "GET" "/products?sort=price:desc" "" "200" "Sort products by price"

echo ""
echo "--- CRUD Operations: Orders ---"
echo ""

# List orders
test_endpoint "GET" "/orders" "" "200" "List orders"

# Filter orders by status
test_endpoint "GET" "/orders?filter[status]=pending" "" "200" "Filter orders by status"

echo ""
echo "--- Raw Query ---"
echo ""

# Execute raw query
test_endpoint "POST" "/query/execute" '{"query": "SELECT COUNT(*) as count FROM users"}' "200" "Execute raw query"

# Invalid query (should fail)
test_endpoint "POST" "/query/execute" '{"query": "DROP TABLE users"}' "400" "Invalid query rejected"

echo ""
echo "==================================="
echo -e "Results: ${GREEN}${PASSED} passed${NC}, ${RED}${FAILED} failed${NC}"
echo "==================================="

if [ $FAILED -gt 0 ]; then
    exit 1
fi
