"""Centralized SQL identifier validation and quoting.

Every dynamic identifier (table or column name) that gets interpolated into a
SQL string MUST pass through :func:`sanitize_identifier` / :func:`quote_identifier`.
Values are always sent as bound parameters and must never go through these
helpers. Keeping this logic in one place avoids divergent, weaker copies
(e.g. ``str.replace``-based "sanitizers") across adapters.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from warp.adapters.outbound.db.dialect import Dialect

# A safe, unquoted SQL identifier: a letter or underscore followed by letters,
# digits, or underscores. Deliberately strict — names from schema introspection
# always satisfy this, while injection payloads do not.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Per-engine additions to that alphabet, cached by the characters allowed.
_EXTENDED_RES: dict[str, re.Pattern[str]] = {}


def _identifier_re(extra: str) -> re.Pattern[str]:
    if not extra:
        return _IDENTIFIER_RE
    cached = _EXTENDED_RES.get(extra)
    if cached is None:
        escaped = re.escape(extra)
        cached = re.compile(rf"^[A-Za-z_][A-Za-z0-9_{escaped}]*$")
        _EXTENDED_RES[extra] = cached
    return cached


def sanitize_identifier(name: str, extra_chars: str = "") -> str:
    """Validate a SQL identifier (table/column name).

    Args:
        name: The identifier to validate.
        extra_chars: Characters this engine additionally allows after the first
            one. Oracle passes ``"$#"``, because they are legal there and the
            data dictionary generates them (an identity column's sequence is
            named ``ISEQ$$_73346``). The double quote is never allowed for any
            engine, which is what keeps a quoted identifier unescapable.

    Returns:
        The identifier unchanged when it is safe.

    Raises:
        ValueError: If the identifier contains anything other than ASCII
            letters, digits, underscores and ``extra_chars`` (and does not
            start with a digit). This is what prevents identifier/SQL injection
            through dynamic table and column names.
    """
    if not isinstance(name, str) or not _identifier_re(extra_chars).match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def quote_identifier(name: str, dialect: str | Dialect = "postgresql") -> str:
    """Validate and quote an identifier for the given SQL dialect.

    Args:
        name: The identifier to validate and quote.
        dialect: Target dialect name or :class:`Dialect` (``mysql``/``mariadb``
            use backticks, ``mssql``/``sqlserver`` square brackets, everything
            else ANSI double quotes).

    Returns:
        The safely quoted identifier.

    Raises:
        ValueError: If the identifier is not valid (see :func:`sanitize_identifier`).
    """
    # Imported lazily: the dialect module builds on sanitize_identifier above.
    from warp.adapters.outbound.db.dialect import dialect_or_generic

    return dialect_or_generic(dialect).quote(name)
