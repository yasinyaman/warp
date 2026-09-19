"""Port: where audit events go."""

from typing import Protocol

from warp.domain.audit import AuditEvent


class AuditSink(Protocol):
    """Records data-access events.

    Implementations must never raise: an audit sink that can fail a request
    turns a logging problem into an outage. They must also never be given
    row values — see :mod:`warp.domain.audit`.
    """

    def record(self, event: AuditEvent) -> None:
        """Record one event."""
        ...


class NullAuditSink:
    """Records nothing (auditing disabled).

    Lives beside the port rather than with the real sinks: it performs no I/O,
    and the inbound adapters need a default without importing an outbound one.
    """

    def record(self, event: AuditEvent) -> None:
        """Discard the event."""
