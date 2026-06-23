"""
Schema analyzer for discovering and analyzing database structures.
"""
from typing import Any

from pydantic import BaseModel, Field, create_model

from ..database.base import DatabaseAdapter
from .models import (
    ColumnSchema,
    DatabaseSchema,
    ForeignKeySchema,
    IndexSchema,
    TableSchema,
)

# Example values for different data types (used in OpenAPI docs)
TYPE_EXAMPLES = {
    # Integer types
    "integer": 1,
    "bigint": 1000000,
    "smallint": 10,
    "serial": 1,
    "bigserial": 1,
    "int": 1,
    "tinyint": 1,
    "mediumint": 100,

    # Float types
    "real": 3.14,
    "double precision": 3.14159265359,
    "numeric": 99.99,
    "decimal": 199.99,
    "float": 3.14,
    "double": 3.14159265359,

    # Boolean
    "boolean": True,
    "bit": True,

    # String types
    "character varying": "example text",
    "varchar": "example text",
    "character": "A",
    "char": "A",
    "text": "This is a longer text content example.",
    "tinytext": "Short text",
    "mediumtext": "Medium length text content",
    "longtext": "Long text content for detailed descriptions",

    # Date/Time types
    "date": "2024-01-15",
    "timestamp": "2024-01-15T10:30:00Z",
    "timestamp with time zone": "2024-01-15T10:30:00+03:00",
    "timestamp without time zone": "2024-01-15T10:30:00",
    "time": "10:30:00",
    "time with time zone": "10:30:00+03:00",
    "time without time zone": "10:30:00",
    "datetime": "2024-01-15T10:30:00",
    "year": 2024,

    # Special types
    "uuid": "550e8400-e29b-41d4-a716-446655440000",
    "json": {"key": "value", "nested": {"data": 123}},
    "jsonb": {"key": "value", "items": [1, 2, 3]},
    "enum": "active",
    "set": "option1,option2",

    # Binary types
    "bytea": "base64_encoded_data",
    "blob": "binary_data",
    "array": [1, 2, 3],
}

# Mapping from database types to Python types for Pydantic model generation
DB_TYPE_MAPPING = {
    # PostgreSQL types
    "integer": int,
    "bigint": int,
    "smallint": int,
    "serial": int,
    "bigserial": int,
    "real": float,
    "double precision": float,
    "numeric": float,
    "decimal": float,
    "boolean": bool,
    "character varying": str,
    "varchar": str,
    "character": str,
    "char": str,
    "text": str,
    "uuid": str,
    "json": dict,
    "jsonb": dict,
    "date": str,
    "timestamp": str,
    "timestamp with time zone": str,
    "timestamp without time zone": str,
    "time": str,
    "time with time zone": str,
    "time without time zone": str,
    "bytea": bytes,
    "array": list,

    # MySQL types
    "int": int,
    "tinyint": int,
    "mediumint": int,
    "float": float,
    "double": float,
    "bit": bool,
    "datetime": str,
    "year": int,
    "enum": str,
    "set": str,
    "blob": bytes,
    "tinyblob": bytes,
    "mediumblob": bytes,
    "longblob": bytes,
    "tinytext": str,
    "mediumtext": str,
    "longtext": str,
}


class SchemaAnalyzer:
    """
    Analyzes database schema and generates Pydantic models.

    Usage:
        analyzer = SchemaAnalyzer(db_adapter)
        schema = await analyzer.analyze()

        # Generate Pydantic model for a table
        UserModel = analyzer.generate_pydantic_model(schema.tables['users'])
    """

    def __init__(
        self,
        db_adapter: DatabaseAdapter,
        excluded_tables: list[str] | None = None
    ):
        """
        Initialize the schema analyzer.

        Args:
            db_adapter: Database adapter instance.
            excluded_tables: List of table names to exclude from analysis.
        """
        self.db = db_adapter
        self.excluded_tables = set(excluded_tables or [])
        self._pydantic_models: dict[str, type[BaseModel]] = {}

    async def analyze(self) -> DatabaseSchema:
        """
        Analyze the complete database schema.

        Returns:
            DatabaseSchema containing all table schemas.
        """
        tables = await self.db.get_tables()
        schema = DatabaseSchema(database_name=self.db.name)

        for table_name in tables:
            if table_name in self.excluded_tables:
                continue

            table_schema = await self.analyze_table(table_name)
            schema.tables[table_name] = table_schema

        return schema

    async def analyze_table(self, table_name: str) -> TableSchema:
        """
        Analyze a single table's schema.

        Args:
            table_name: Name of the table to analyze.

        Returns:
            TableSchema with complete table metadata.
        """
        raw_schema = await self.db.get_table_schema(table_name)

        # Convert raw schema to typed models
        columns = [
            ColumnSchema(**col) for col in raw_schema.get("columns", [])
        ]

        foreign_keys = [
            ForeignKeySchema(**fk) for fk in raw_schema.get("foreign_keys", [])
        ]

        indexes = [
            IndexSchema(**idx) for idx in raw_schema.get("indexes", [])
        ]

        return TableSchema(
            table_name=table_name,
            columns=columns,
            primary_key=raw_schema.get("primary_key"),
            foreign_keys=foreign_keys,
            indexes=indexes
        )

    def generate_pydantic_model(
        self,
        table_schema: TableSchema,
        model_name: str | None = None,
        for_create: bool = False,
        for_update: bool = False
    ) -> type[BaseModel]:
        """
        Generate a Pydantic model from table schema.

        Args:
            table_schema: TableSchema to convert.
            model_name: Optional custom model name.
            for_create: If True, excludes auto-generated fields.
            for_update: If True, makes all fields optional.

        Returns:
            Dynamically created Pydantic model class.
        """
        if model_name is None:
            suffix = ""
            if for_create:
                suffix = "Create"
            elif for_update:
                suffix = "Update"
            model_name = self._to_pascal_case(table_schema.table_name) + suffix

        # Check cache
        cache_key = f"{model_name}_{for_create}_{for_update}"
        if cache_key in self._pydantic_models:
            return self._pydantic_models[cache_key]

        fields: dict[str, Any] = {}

        for col in table_schema.columns:
            # Skip auto-generated columns for create models
            if for_create:
                if col.extra and "auto_increment" in col.extra.lower():
                    continue
                if col.default and ("nextval" in col.default.lower() or "identity" in col.default.lower()):
                    continue

            python_type = self._get_python_type(col)
            example_value = self._get_example_value(col)

            # Determine if field is optional
            is_optional = col.nullable or col.default is not None or for_update

            # Build field description
            description = self._build_field_description(col)

            if is_optional:
                python_type = python_type | None
                field_info = Field(
                    default=None,
                    description=description,
                    json_schema_extra={"example": example_value} if example_value else None
                )
            else:
                field_info = Field(
                    ...,
                    description=description,
                    json_schema_extra={"example": example_value} if example_value else None
                )

            fields[col.name] = (python_type, field_info)

        # Create the model
        model = create_model(model_name, **fields)
        self._pydantic_models[cache_key] = model

        return model

    def generate_crud_models(
        self,
        table_schema: TableSchema
    ) -> dict[str, type[BaseModel]]:
        """
        Generate all CRUD-related Pydantic models for a table.

        Returns dictionary with:
        - 'base': Full model with all fields
        - 'create': Model for creating records
        - 'update': Model for updating records (all optional)
        - 'response': Model for API responses
        """
        base_name = self._to_pascal_case(table_schema.table_name)

        return {
            "base": self.generate_pydantic_model(table_schema),
            "create": self.generate_pydantic_model(
                table_schema, f"{base_name}Create", for_create=True
            ),
            "update": self.generate_pydantic_model(
                table_schema, f"{base_name}Update", for_update=True
            ),
            "response": self.generate_pydantic_model(
                table_schema, f"{base_name}Response"
            ),
        }

    def _get_python_type(self, col: ColumnSchema) -> type:
        """Map database column type to Python type."""
        db_type = col.type.lower()

        # Check direct mapping
        if db_type in DB_TYPE_MAPPING:
            return DB_TYPE_MAPPING[db_type]

        # Check udt_name for PostgreSQL
        if col.udt_name:
            udt = col.udt_name.lower()
            if udt in DB_TYPE_MAPPING:
                return DB_TYPE_MAPPING[udt]
            # Handle array types
            if udt.startswith("_"):
                return list

        # Check full_type for MySQL
        if col.full_type:
            full = col.full_type.lower()
            if "tinyint(1)" in full:
                return bool

        # Default to string
        return str

    def _get_example_value(self, col: ColumnSchema) -> Any:
        """Get an example value for the column based on its type and name."""
        db_type = col.type.lower()
        col_name = col.name.lower()

        # Smart examples based on column name patterns
        name_based_examples = {
            "email": "user@example.com",
            "mail": "user@example.com",
            "phone": "+90 555 123 4567",
            "tel": "+90 555 123 4567",
            "mobile": "+90 555 123 4567",
            "url": "https://example.com",
            "website": "https://example.com",
            "link": "https://example.com/page",
            "name": "John Doe",
            "first_name": "John",
            "firstname": "John",
            "last_name": "Doe",
            "lastname": "Doe",
            "username": "johndoe",
            "user_name": "johndoe",
            "password": "********",
            "title": "Sample Title",
            "description": "This is a sample description text.",
            "content": "This is the main content of the item.",
            "body": "This is the body text content.",
            "address": "123 Main Street, City, Country",
            "city": "Istanbul",
            "country": "Turkey",
            "zip": "34000",
            "postal_code": "34000",
            "status": "active",
            "state": "pending",
            "type": "standard",
            "category": "general",
            "price": 99.99,
            "amount": 150.00,
            "total": 299.99,
            "quantity": 5,
            "count": 10,
            "age": 25,
            "rating": 4.5,
            "score": 85,
            "lat": 41.0082,
            "latitude": 41.0082,
            "lng": 28.9784,
            "longitude": 28.9784,
            "ip": "192.168.1.1",
            "ip_address": "192.168.1.1",
            "color": "#FF5733",
            "image": "https://example.com/image.jpg",
            "avatar": "https://example.com/avatar.png",
            "photo": "https://example.com/photo.jpg",
            "file": "document.pdf",
            "filename": "report_2024.xlsx",
        }

        # Check for name-based example first
        for pattern, example in name_based_examples.items():
            if pattern in col_name:
                return example

        # Check for ID fields
        if col_name == "id" or col_name.endswith("_id"):
            return 1

        # Check for created/updated timestamps
        if "created" in col_name or "updated" in col_name or "modified" in col_name:
            return "2024-01-15T10:30:00Z"

        # Check for boolean-like names
        if col_name.startswith("is_") or col_name.startswith("has_") or col_name.startswith("can_"):
            return True

        # Fall back to type-based example
        if db_type in TYPE_EXAMPLES:
            return TYPE_EXAMPLES[db_type]

        # Check udt_name for PostgreSQL
        if col.udt_name and col.udt_name.lower() in TYPE_EXAMPLES:
            return TYPE_EXAMPLES[col.udt_name.lower()]

        return "example"

    def _build_field_description(self, col: ColumnSchema) -> str:
        """Build a description string for the field."""
        parts = []

        # Type info
        type_str = col.full_type or col.type
        parts.append(f"Type: {type_str}")

        # Constraints
        if not col.nullable:
            parts.append("Required")
        else:
            parts.append("Optional")

        # Max length
        if col.max_length:
            parts.append(f"Max length: {col.max_length}")

        # Default value
        if col.default:
            # Clean up default value display
            default_display = col.default
            if "nextval" in default_display.lower():
                default_display = "Auto-generated"
            parts.append(f"Default: {default_display}")

        return " | ".join(parts)

    @staticmethod
    def _to_pascal_case(snake_str: str) -> str:
        """Convert snake_case to PascalCase."""
        components = snake_str.split('_')
        return ''.join(x.title() for x in components)
