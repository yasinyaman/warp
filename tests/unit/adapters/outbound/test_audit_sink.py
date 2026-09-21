"""Tests for the audit sinks."""

import json
import logging

from warp.adapters.outbound.audit import LoggingAuditSink
from warp.adapters.outbound.audit.log_sink import AUDIT_LOGGER
from warp.application.ports.audit import NullAuditSink
from warp.domain.audit import AuditEvent

EVENT = AuditEvent(
    action="read",
    database="shop",
    table="users",
    actor="acme",
    request_id="r1",
    row_count=2,
    masked_columns=("email",),
)


def test_the_null_sink_records_nothing():
    NullAuditSink().record(EVENT)  # must not raise


class TestLoggingSink:
    def test_it_logs_to_a_dedicated_logger(self, caplog):
        # A separate logger so audit output can be routed and retained apart
        # from application logs, without filtering on message text.
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            LoggingAuditSink().record(EVENT)
        assert caplog.records
        assert caplog.records[0].name == AUDIT_LOGGER
        assert caplog.records[0].extra_fields["table"] == "users"

    def test_it_appends_json_lines_to_a_file(self, tmp_path):
        path = tmp_path / "audit" / "trail.jsonl"
        sink = LoggingAuditSink(str(path))
        sink.record(EVENT)
        sink.record(EVENT)

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["event"] == "data_access"
        assert first["actor"] == "acme"
        assert first["masked_columns"] == ["email"]

    def test_the_file_is_appended_not_rewritten(self, tmp_path):
        # A restart must not truncate the trail.
        path = tmp_path / "trail.jsonl"
        LoggingAuditSink(str(path)).record(EVENT)
        LoggingAuditSink(str(path)).record(EVENT)
        assert len(path.read_text().strip().splitlines()) == 2

    def test_the_parent_directory_is_created(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "trail.jsonl"
        LoggingAuditSink(str(path)).record(EVENT)
        assert path.exists()

    def test_an_unwritable_path_does_not_fail_the_request(self, tmp_path, caplog):
        # An audit sink that can fail a request turns a logging problem into
        # an outage.
        blocked = tmp_path / "file.txt"
        blocked.write_text("not a directory")
        sink = LoggingAuditSink(str(blocked / "trail.jsonl"))
        with caplog.at_level(logging.ERROR):
            sink.record(EVENT)
        assert "audit" in caplog.text.lower()

    def test_no_file_configured_still_logs(self, caplog):
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            LoggingAuditSink().record(EVENT)
        assert caplog.records
