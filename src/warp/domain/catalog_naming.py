"""Catalog/table/column naming rules (pure).

- `validate_catalog_name`: a catalog name doubles as a directory name in the file
  store, so only plain identifiers are accepted.
- `normalize_name` / `names_are_similar`: heuristics used to find related tables and
  columns across catalogs.
"""

import re

from warp.domain.errors import InvalidCatalogNameError

# A catalog name becomes a directory name under the store root. Only plain
# identifiers are allowed: no path separators, no dot segments, no leading "_"
# (reserved for store-internal files such as the index), max 64 characters.
CATALOG_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_CATALOG_NAME_RE = re.compile(CATALOG_NAME_PATTERN)


def validate_catalog_name(name: str) -> str:
    """Return `name` if it is a safe catalog identifier, else raise.

    Raises:
        InvalidCatalogNameError: If the name could escape or collide inside
            the storage directory.
    """
    if not isinstance(name, str) or not _CATALOG_NAME_RE.fullmatch(name):
        raise InvalidCatalogNameError(str(name))
    return name


def normalize_name(name: str) -> str:
    """Normalize a table/column name for comparison."""
    n = name.lower().strip()
    for prefix in ("tbl_", "t_", "tb_", "dim_", "fact_"):
        if n.startswith(prefix):
            n = n[len(prefix) :]
            break
    for suffix in ("_id", "_key", "_fk", "_pk"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    if n.endswith("ies"):
        n = n[:-3] + "y"
    elif n.endswith("ses"):
        n = n[:-2]
    elif n.endswith("s") and not n.endswith("ss"):
        n = n[:-1]
    return n


def names_are_similar(name1: str, name2: str) -> bool:
    """Check if two normalized names are similar."""
    if name1 == name2:
        return True
    return bool(len(name1) >= 3 and len(name2) >= 3 and (name1 in name2 or name2 in name1))
