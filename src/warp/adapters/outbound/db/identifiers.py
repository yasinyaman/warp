"""Centralized SQL identifier validation and quoting.

Every dynamic identifier (table or column name) that gets interpolated into a
SQL string MUST pass through :func:`sanitize_identifier` / :func:`quote_identifier`.
Values are always sent as bound parameters and must never go through these
helpers. Keeping this logic in one place avoids divergent, weaker copies
(e.g. ``str.replace``-based "sanitizers") across adapters.
"""

import re

# A safe, unquoted SQL identifier: a letter or underscore followed by letters,
# digits, or underscores. Deliberately strict — names from schema introspection
# always satisfy this, while injection payloads do not.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_identifier(name: str) -> str:
    """Validate a SQL identifier (table/column name).

    Args:
        name: The identifier to validate.

    Returns:
        The identifier unchanged when it is safe.

    Raises:
        ValueError: If the identifier contains anything other than ASCII
            letters, digits, and underscores (and does not start with a digit).
            This is what prevents identifier/SQL injection through dynamic
            table and column names.
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def quote_identifier(name: str, dialect: str = "postgresql") -> str:
    """Validate and quote an identifier for the given SQL dialect.

    Args:
        name: The identifier to validate and quote.
        dialect: Target SQL dialect (``"mysql"``/``"mariadb"`` use backticks,
            everything else uses ANSI double quotes).

    Returns:
        The safely quoted identifier.

    Raises:
        ValueError: If the identifier is not valid (see :func:`sanitize_identifier`).
    """
    safe = sanitize_identifier(name)
    if dialect in ("mysql", "mariadb"):
        return f"`{safe}`"
    return f'"{safe}"'
