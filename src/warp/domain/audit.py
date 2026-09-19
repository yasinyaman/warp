"""The audit record: who read or changed what, and whether it was restricted.

One event per data-touching request. The point of recording *whether* a row
filter or a mask applied is that an audit log which cannot distinguish "saw
everything" from "saw their own tenant, with the email column masked" cannot
answer the only question anyone asks it afterwards.

What an event must never carry is the data itself: no row values, no filter
literals, no SQL parameters. An audit trail that quotes the values it is
auditing becomes a second copy of the data, in a file that usually has weaker
access controls and a longer retention than the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

AuditAction = Literal["read", "create", "update", "delete", "export", "query"]

#: Actions that change data, kept separately because retention rules and
#: alerting usually treat them differently from reads.
WRITE_ACTIONS: frozenset[str] = frozenset({"create", "update", "delete"})


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One recorded data access."""

    action: AuditAction
    database: str
    table: str
    #: The API key's name, or ``None`` when authentication is off. ``None`` is
    #: itself worth recording: it means the request could not be attributed.
    actor: str | None = None
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    request_id: str = ""
    status: int = 200
    row_count: int | None = None
    #: Whether a row policy narrowed this request, and which columns were
    #: masked — the difference between "read the table" and "read their slice
    #: of it, with two columns hidden".
    row_filtered: bool = False
    masked_columns: tuple[str, ...] = ()
    #: Column names the caller filtered on. Names only, never the values.
    filtered_columns: tuple[str, ...] = ()
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_write(self) -> bool:
        """Whether this event changed data."""
        return self.action in WRITE_ACTIONS

    @property
    def restricted(self) -> bool:
        """Whether anything was withheld from the caller."""
        return self.row_filtered or bool(self.masked_columns)

    def as_dict(self) -> dict[str, Any]:
        """The event as a flat JSON-safe mapping."""
        return {
            "event": "data_access",
            "at": self.at.isoformat(),
            "action": self.action,
            "database": self.database,
            "table": self.table,
            "actor": self.actor,
            "tenant": self.tenant,
            "roles": list(self.roles),
            "request_id": self.request_id,
            "status": self.status,
            "row_count": self.row_count,
            "row_filtered": self.row_filtered,
            "masked_columns": list(self.masked_columns),
            "filtered_columns": list(self.filtered_columns),
            "restricted": self.restricted,
            "is_write": self.is_write,
        }

    def summary(self) -> str:
        """A one-line human-readable form, for a text log."""
        who = self.actor or "anonymous"
        where = f"{self.database}.{self.table}"
        rows = "" if self.row_count is None else f" rows={self.row_count}"
        marks = []
        if self.row_filtered:
            marks.append("row-filtered")
        if self.masked_columns:
            marks.append(f"masked={','.join(self.masked_columns)}")
        suffix = f" [{'; '.join(marks)}]" if marks else ""
        return f"{who} {self.action} {where}{rows} status={self.status}{suffix}"
