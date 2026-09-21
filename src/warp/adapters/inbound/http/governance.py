"""What the HTTP layer needs in order to restrict and record data access.

Masking and auditing travel together because every route that applies one
should apply the other: a response that withheld columns and a log line saying
which columns were withheld are two halves of the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from warp.application.ports.audit import AuditSink, NullAuditSink
from warp.domain.masking import NO_MASKING, CatalogMasking


@dataclass(frozen=True)
class Governance:
    """Masking rules and the audit sink for one mounted database."""

    masking: CatalogMasking = field(default_factory=lambda: NO_MASKING)
    audit: AuditSink = field(default_factory=NullAuditSink)


NO_GOVERNANCE = Governance()
"""Nothing configured: no masking, and events go nowhere."""
