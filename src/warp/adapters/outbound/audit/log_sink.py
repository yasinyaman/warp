"""Audit sinks that write to a logger or a file."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from warp.domain.audit import AuditEvent

logger = logging.getLogger(__name__)

#: A dedicated logger so audit output can be routed (and retained) separately
#: from application logs without filtering on message text.
AUDIT_LOGGER = "warp.audit"


class LoggingAuditSink:
    """Writes one JSON object per event.

    Events go to the ``warp.audit`` logger, and additionally to ``path`` when
    one is configured — appended, never rewritten, so the file is an append-only
    record even if the process restarts.
    """

    def __init__(self, path: str | None = None) -> None:
        """Open the audit file, if one is configured."""
        self._logger = logging.getLogger(AUDIT_LOGGER)
        self._path = Path(path) if path else None
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.error(f"Cannot create the audit directory {self._path.parent}: {e}")
                self._path = None

    def record(self, event: AuditEvent) -> None:
        """Record one event, and never let a logging failure fail the request."""
        payload = event.as_dict()
        try:
            self._logger.info(event.summary(), extra={"extra_fields": payload})
        except Exception as e:  # pragma: no cover - a broken logging config
            logger.error(f"Audit logging failed: {e}")
        if self._path is None:
            return
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
        except OSError as e:
            logger.error(f"Could not append to the audit file {self._path}: {e}")
