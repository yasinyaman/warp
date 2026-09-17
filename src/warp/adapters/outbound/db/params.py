"""Named-parameter binding for raw SQL.

Callers write ``:name`` placeholders and pass a dict of values; this module
rewrites the statement into the driver's positional form (``$1`` for asyncpg,
``%s`` for aiomysql, ``?`` for pyodbc) and returns the values in the matching
order.

Rules:
- A name is ``:`` followed by an identifier and must not be preceded by a word
  character or another ``:``, so ``x::int`` casts and ``a:b`` are untouched.
- Single-quoted string literals are skipped entirely.
- A name may be used several times (numbered dialects reuse the same ``$N``;
  the others repeat the placeholder and the value).
- Every referenced name must be provided and every provided name must be
  referenced, otherwise ``ValueError`` (a typo would otherwise bind silently).
- For dialects whose driver applies printf-style formatting whenever parameters
  are given (MySQL), a literal ``%`` outside a placeholder is escaped to ``%%``.
"""

import re
from typing import Any

from warp.adapters.outbound.db.dialect import Dialect, get_dialect

_TOKEN = re.compile(
    r"""
    ('(?:[^']|'')*')          # 1: single-quoted literal ('' escapes a quote)
  | (::)                      # 2: PostgreSQL cast operator
  | (?<![\w:]):([A-Za-z_]\w*) # 3: :name placeholder
  | (%)                       # 4: percent sign (MySQL formatting hazard)
    """,
    re.VERBOSE,
)


def _require_no_placeholders(query: str) -> None:
    """Without values, any placeholder outside literals/casts is a missing value.

    Raising here turns a typo into a 400 from the raw endpoint instead of a
    driver syntax error (500).
    """
    for m in _TOKEN.finditer(query):
        if m.group(3) is not None:
            raise ValueError(f"Missing value for query parameter :{m.group(3)}")


def bind_named_params(
    query: str, params: dict[str, Any] | None, dialect: str | Dialect
) -> tuple[str, list[Any]]:
    """Rewrite ``:name`` placeholders into the dialect's positional form.

    Args:
        query: SQL text with ``:name`` placeholders.
        params: Values by name. ``None``/empty leaves the query untouched.
        dialect: Dialect name or :class:`Dialect`: ``postgresql`` (``$N``),
            ``mysql`` (``%s``), ``mssql``/``odbc`` (``?``).

    Returns:
        The rewritten SQL and the positional values.

    Raises:
        ValueError: On an unknown dialect, a placeholder without a value, or a
            value without a placeholder.
    """
    resolved = get_dialect(dialect)
    if not params:
        _require_no_placeholders(query)
        return query, []

    positional: list[Any] = []
    numbered: dict[str, int] = {}
    used: set[str] = set()
    escape_percent = resolved.escape_percent
    reuse_placeholder = resolved.placeholder_style == "numbered"

    def replace(m: re.Match[str]) -> str:
        literal, cast, name, percent = m.groups()
        if literal is not None:
            return literal.replace("%", "%%") if escape_percent else literal
        if cast is not None:
            return cast
        if percent is not None:
            return "%%" if escape_percent else "%"
        assert name is not None
        if name not in params:
            raise ValueError(f"Missing value for query parameter :{name}")
        used.add(name)
        if not reuse_placeholder:
            positional.append(params[name])
            return resolved.placeholder(len(positional))
        if name not in numbered:
            numbered[name] = len(positional) + 1
            positional.append(params[name])
        return resolved.placeholder(numbered[name])

    rewritten = _TOKEN.sub(replace, query)

    unused = sorted(set(params) - used)
    if unused:
        raise ValueError(f"Unused query parameter(s): {', '.join(unused)}")
    return rewritten, positional
