# Warp Engine

Auto-generates REST CRUD endpoints from database schema (PostgreSQL/MySQL).

## Features

- **Auto-discovery** - Automatically discovers database tables and generates API endpoints
- **Full CRUD** - Create, Read, Update, Delete operations for all tables
- **Filtering** - Filter results using query parameters with operators (eq, gt, lt, in, etc.)
- **Sorting** - Sort results by any column, ascending or descending
- **Pagination** - Built-in pagination with configurable limits
- **Raw Queries** - Execute raw SQL queries (when enabled)
- **Multi-DB Support** - PostgreSQL and MySQL support
- **Production Ready** - Connection retry, health checks, structured logging

## Quick Start

### Using Docker Compose

```bash
# Clone the repository
git clone https://github.com/yourorg/warp.git
cd warp

# Start all services (API + PostgreSQL + MySQL)
docker-compose up -d

# API is available at http://localhost:8000
# Documentation at http://localhost:8000/docs
```

### Using pip

```bash
pip install warp

# Set environment variables
export DB_HOST=localhost
export DB_NAME=mydb
export DB_USER=postgres
export DB_PASS=secret

# Run the server
warp
```

## Configuration

Create a `config/database.yaml` file:

```yaml
databases:
  - name: primary_db
    type: postgresql
    host: ${DB_HOST:localhost}
    port: ${DB_PORT:5432}
    database: ${DB_NAME:myapp}
    username: ${DB_USER:postgres}
    password: ${DB_PASS:}
    options:
      pool_size: 10
      ssl: false

settings:
  auto_discover_tables: true
  excluded_tables:
    - migrations
    - alembic_version
  pagination:
    default_limit: 50
    max_limit: 1000
  enable_raw_query: true
  api_prefix: /api/v1
```

## API Usage

Once running, Warp automatically creates endpoints for each discovered table.

### List Records

```bash
GET /api/v1/{table}
GET /api/v1/users
GET /api/v1/products
```

### Get Single Record

```bash
GET /api/v1/{table}/{id}
GET /api/v1/users/123
```

### Create Record

```bash
POST /api/v1/{table}
Content-Type: application/json

{
  "name": "John Doe",
  "email": "john@example.com"
}
```

### Update Record

```bash
PUT /api/v1/{table}/{id}
Content-Type: application/json

{
  "name": "Jane Doe"
}
```

### Delete Record

```bash
DELETE /api/v1/{table}/{id}
```

### Filtering

```bash
# Exact match
GET /api/v1/users?filter[status]=active

# Comparison operators
GET /api/v1/products?filter[price][gte]=100&filter[price][lte]=500

# IN operator
GET /api/v1/orders?filter[status][in]=pending,processing

# NULL check
GET /api/v1/users?filter[deleted_at][is_null]=true
```

### Sorting

```bash
# Single column ascending
GET /api/v1/users?sort=name:asc

# Single column descending
GET /api/v1/products?sort=price:desc

# Multiple columns
GET /api/v1/orders?sort=status:asc,created_at:desc

# Shorthand (prefix with - for descending)
GET /api/v1/users?sort=-created_at
```

### Pagination

```bash
# Default pagination
GET /api/v1/users?limit=20&offset=0

# Page 3 with 20 items per page
GET /api/v1/users?limit=20&offset=40
```

### Raw Queries

```bash
POST /api/v1/query
Content-Type: application/json

{
  "sql": "SELECT * FROM users WHERE status = $1",
  "params": ["active"]
}
```

## Health Endpoints

```bash
GET /health  # Full health check with database status
GET /ready   # Kubernetes readiness probe
GET /live    # Kubernetes liveness probe
GET /info    # API information and discovered tables
```

## Development

### Setup

```bash
# Clone repository
git clone https://github.com/yourorg/warp.git
cd warp

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with hot-reload
APP_ENV=development python -m warp.main
```

### Project Structure

```
warp/
├── src/warp/
│   ├── api/           # API routers and CRUD operations
│   ├── config/        # Configuration management
│   ├── core/          # Exceptions and logging
│   ├── database/      # Database adapters (PostgreSQL, MySQL)
│   ├── schema/        # Schema discovery and analysis
│   └── utils/         # Filtering, pagination, sorting
├── config/            # Configuration files
├── docker/            # Docker-related files
└── tests/             # Test suite
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `development` | Environment (development/production) |
| `API_HOST` | `0.0.0.0` | Host to bind |
| `API_PORT` | `8000` | Port to bind |
| `LOG_LEVEL` | `INFO` | Log level |
| `LOG_FORMAT` | `colored` | Log format (colored/json) |
| `CONFIG_PATH` | `config/database.yaml` | Configuration file path |
| `CORS_ORIGINS` | `*` | Allowed CORS origins |

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
