"""Tests for autopsy.collectors.cloudwatch — CloudWatch Logs Insights collector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import botocore.exceptions

from autopsy.collectors.cloudwatch import (
    CloudWatchCollector,
    _extract_template,
    _result_row_to_entry,
    _truncate_message,
)
from autopsy.collectors.base import CollectedData
from autopsy.utils.errors import (
    AWSAuthError,
    AWSPermissionError,
    CollectorError,
    NoDataError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aws_config(
    region: str = "us-east-1",
    log_groups: list[str] | None = None,
    time_window: int = 30,
    profile: str | None = None,
) -> dict:
    """Build minimal AWS config dict for collector."""
    if log_groups is None:
        log_groups = ["/aws/lambda/my-api"]
    out = {
        "region": region,
        "log_groups": log_groups,
        "time_window": time_window,
    }
    if profile is not None:
        out["profile"] = profile
    return out


def _insights_row(timestamp: str = "2026-03-06T10:00:00.000Z", message: str = "ERROR") -> list[dict]:
    """One Logs Insights result row (list of field/value dicts)."""
    return [
        {"field": "@timestamp", "value": timestamp},
        {"field": "@message", "value": message},
        {"field": "@logStream", "value": "stream1"},
    ]


def _mock_logs_client(
    describe_pages: list[dict] | None = None,
    start_query_id: str = "query-123",
    get_results_status: str = "Complete",
    get_results_rows: list[list[dict]] | None = None,
    get_results_side_effect=None,
):
    """Build a mock logs client with describe_log_groups and query behavior."""
    if describe_pages is None:
        describe_pages = [{"logGroups": [{"logGroupName": "/aws/lambda/my-api"}]}]
    if get_results_rows is None:
        get_results_rows = [_insights_row(message="ERROR: connection refused")]

    client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(describe_pages)
    client.get_paginator.return_value = paginator
    client.start_query.return_value = {"queryId": start_query_id}
    if get_results_side_effect is not None:
        client.get_query_results.side_effect = get_results_side_effect
    else:
        client.get_query_results.return_value = {
            "status": get_results_status,
            "results": get_results_rows,
        }
    return client


# ---------------------------------------------------------------------------
# _extract_template
# ---------------------------------------------------------------------------


class TestExtractTemplate:
    """Template extraction for deduplication."""

    def test_uuid_replaced(self) -> None:
        msg = "Request id 550e8400-e29b-41d4-a716-446655440000 failed"
        assert "<UUID>" in _extract_template(msg)
        assert "550e8400" not in _extract_template(msg)

    def test_ip_replaced(self) -> None:
        msg = "Connection from 192.168.1.100 refused"
        assert "<IP>" in _extract_template(msg)
        assert "192.168.1.100" not in _extract_template(msg)

    def test_timestamp_replaced(self) -> None:
        msg = "Event at 2026-03-06T10:00:00.000Z"
        assert "<TS>" in _extract_template(msg)
        assert "2026-03-06" not in _extract_template(msg)

    def test_long_numbers_replaced(self) -> None:
        msg = "Order 12345678 failed"
        assert "<NUM>" in _extract_template(msg)
        assert "12345678" not in _extract_template(msg)

    def test_hex_replaced(self) -> None:
        msg = "Hash abcdef0123456789 invalid"
        assert "<HEX>" in _extract_template(msg)
        assert "abcdef0123456789" not in _extract_template(msg)

    def test_short_numbers_unchanged(self) -> None:
        msg = "Retry 3 of 5"
        result = _extract_template(msg)
        assert "3" in result and "5" in result

    def test_placeholder_only_diff(self) -> None:
        a = _extract_template("Error at 2026-03-06T10:00:00Z id 12345")
        b = _extract_template("Error at 2026-03-07T11:00:00Z id 67890")
        assert a == b


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestCollectHappyPath:
    """Collect returns valid CollectedData with mocked boto3."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_collected_data_shape(
        self, mock_session: MagicMock
    ) -> None:
        client = _mock_logs_client(
            get_results_rows=[
                _insights_row(message="ERROR: NullPointerException"),
                _insights_row(timestamp="2026-03-06T10:01:00.000Z", message="ERROR: timeout"),
            ],
        )
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()
        result = collector.collect(_aws_config())

        assert isinstance(result, CollectedData)
        assert result.source == "cloudwatch"
        assert result.data_type == "logs"
        assert result.entry_count == 2
        assert len(result.entries) == 2
        assert result.time_range[0] <= result.time_range[1]
        assert "error|exception" in result.raw_query.lower() or "Logs Insights" in result.raw_query
        assert result.entries[0].get("@message") == "ERROR: NullPointerException"
        assert result.entries[0].get("@timestamp")
        assert result.entries[0].get("occurrences", 1) == 1


# ---------------------------------------------------------------------------
# Dedup pipeline
# ---------------------------------------------------------------------------


class TestDedupPipeline:
    """Stage 2: duplicate messages merged with occurrences count."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_duplicates_merged_with_occurrences(
        self, mock_session: MagicMock
    ) -> None:
        # 20 rows: 5 distinct message structures, 4 copies each (same template, different values)
        templates = [
            "ERROR: request 10000 failed at 2026-03-06T10:00:00Z",
            "ERROR: connection from 192.168.1.1 refused",
            "ERROR: timeout after 5000 ms",
            "ERROR: code 500 exception in handler",
            "ERROR: id abcdef0123456789 not found",
        ]
        rows = []
        for msg in templates:
            for _ in range(4):
                rows.append(_insights_row(message=msg))
        # One more with a different structure (so 6th unique template)
        rows.append(_insights_row(message="FATAL: panic in goroutine"))

        client = _mock_logs_client(get_results_rows=rows)
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()
        result = collector.collect(_aws_config())

        # 5 templates (4 copies each) + 1 unique = 6 unique entries, 21 total rows
        assert result.entry_count == 21
        assert len(result.entries) == 6
        occurrences = [e.get("occurrences", 1) for e in result.entries]
        assert max(occurrences) >= 2
        assert sum(occurrences) == 21


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


class TestTruncation:
    """Stage 3: messages capped at 500 chars; stack traces to 5 frames."""

    def test_truncate_message_cap(self) -> None:
        long_msg = "x" * 1000
        out = _truncate_message(long_msg)
        assert len(out) <= 501  # 500 + "…"
        assert out.endswith("…")

    def test_stack_trace_trimmed(self) -> None:
        lines = [
            "Traceback (most recent call last):",
            "  File \"/app/handler.py\", line 42, in run",
            "    raise ValueError('bad')",
            "  File \"/app/lib.py\", line 1, in helper",
            "  File \"/app/lib.py\", line 2, in helper",
            "  File \"/app/lib.py\", line 3, in helper",
            "  File \"/app/lib.py\", line 4, in helper",
            "  File \"/app/lib.py\", line 5, in helper",
        ]
        msg = "\n".join(lines)
        out = _truncate_message(msg)
        # Should keep first line + at most 5 "  File " frame lines
        frame_count = out.count("  File ")
        assert frame_count <= 5
        assert "Traceback" in out or "ValueError" in out

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_collect_truncates_long_messages(
        self, mock_session: MagicMock
    ) -> None:
        client = _mock_logs_client(
            get_results_rows=[
                _insights_row(message="A" * 1200),
            ],
        )
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()
        result = collector.collect(_aws_config())

        assert len(result.entries) == 1
        assert len(result.entries[0]["@message"]) <= 501


# ---------------------------------------------------------------------------
# Token budget
# ---------------------------------------------------------------------------


class TestTokenBudget:
    """Stage 4: hard cap at 6000 tokens; truncated=True when eviction occurs."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_over_budget_sets_truncated(
        self, mock_session: MagicMock
    ) -> None:
        # 100 unique templates (different "type XX") so no dedup; ~300 chars each ≈ 7500 tokens
        rows = [
            _insights_row(
                message=f"ERROR: type {chr(65 + i % 26)}{chr(65 + (i // 26) % 26)} " + "x" * 280
            )
            for i in range(100)
        ]
        client = _mock_logs_client(get_results_rows=rows)
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()
        result = collector.collect(_aws_config())

        total_chars = sum(
            len(e.get("@message", "")) + len(e.get("@timestamp", ""))
            for e in result.entries
        )
        estimated_tokens = total_chars // 4
        assert estimated_tokens <= 6500  # allow some slack for our estimate
        assert result.truncated is True
        assert len(result.entries) < 100


# ---------------------------------------------------------------------------
# Empty results
# ---------------------------------------------------------------------------


class TestEmptyResults:
    """NoDataError when query returns zero results."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_zero_results_raises_no_data_error(
        self, mock_session: MagicMock
    ) -> None:
        client = _mock_logs_client(get_results_rows=[])
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()

        with pytest.raises(NoDataError) as exc_info:
            collector.collect(_aws_config(time_window=15))

        assert "No error-level logs" in exc_info.value.message
        assert "15" in exc_info.value.message or "minutes" in exc_info.value.message
        assert "log_groups" in exc_info.value.hint.lower() or "config" in exc_info.value.hint


# ---------------------------------------------------------------------------
# Auth failure
# ---------------------------------------------------------------------------


class TestAuthFailure:
    """NoCredentialsError → AWSAuthError with correct hint."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_no_credentials_raises_auth_error(
        self, mock_session: MagicMock
    ) -> None:
        mock_session.return_value.client.side_effect = (
            botocore.exceptions.NoCredentialsError()
        )

        collector = CloudWatchCollector()

        with pytest.raises(AWSAuthError) as exc_info:
            collector.validate_config(_aws_config())

        assert "credentials not found" in exc_info.value.message.lower()
        assert "aws configure" in exc_info.value.hint.lower() or "AWS_PROFILE" in exc_info.value.hint


# ---------------------------------------------------------------------------
# Permission failure
# ---------------------------------------------------------------------------


class TestPermissionFailure:
    """ClientError AccessDenied → AWSPermissionError."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_access_denied_raises_permission_error(
        self, mock_session: MagicMock
    ) -> None:
        client = MagicMock()
        client.get_paginator.return_value.paginate.side_effect = (
            botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Access Denied"}},
                "DescribeLogGroups",
            )
        )
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()

        with pytest.raises(AWSPermissionError) as exc_info:
            collector.validate_config(_aws_config())

        assert "permission" in exc_info.value.message.lower()
        assert "logs:DescribeLogGroups" in exc_info.value.hint
        assert "logs:StartQuery" in exc_info.value.hint


# ---------------------------------------------------------------------------
# Expired creds
# ---------------------------------------------------------------------------


class TestExpiredCreds:
    """ClientError ExpiredToken → AWSAuthError."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_expired_token_raises_auth_error(
        self, mock_session: MagicMock
    ) -> None:
        client = MagicMock()
        client.get_paginator.return_value.paginate.side_effect = (
            botocore.exceptions.ClientError(
                {"Error": {"Code": "ExpiredToken", "Message": "Token expired"}},
                "DescribeLogGroups",
            )
        )
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()

        with pytest.raises(AWSAuthError) as exc_info:
            collector.validate_config(_aws_config())

        assert "expired" in exc_info.value.message.lower()
        assert "sso login" in exc_info.value.hint.lower() or "session token" in exc_info.value.hint.lower()


# ---------------------------------------------------------------------------
# Query timeout
# ---------------------------------------------------------------------------


class TestQueryTimeout:
    """get_query_results never completes → CollectorError."""

    @patch("autopsy.collectors.cloudwatch.time.sleep")  # speed up test
    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_poll_timeout_raises_collector_error(
        self, mock_session: MagicMock, mock_sleep: MagicMock
    ) -> None:
        def always_running():
            while True:
                yield {"status": "Running", "results": []}

        client = _mock_logs_client(get_results_side_effect=always_running())
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()

        with pytest.raises(CollectorError) as exc_info:
            collector.collect(_aws_config())

        assert "timeout" in exc_info.value.message.lower() or "30" in exc_info.value.message


# ---------------------------------------------------------------------------
# _result_row_to_entry
# ---------------------------------------------------------------------------


class TestResultRowToEntry:
    """Conversion from Logs Insights row to entry dict."""

    def test_maps_timestamp_message_logstream(self) -> None:
        row = _insights_row(timestamp="2026-03-06T10:00:00Z", message="ERROR: fail")
        entry = _result_row_to_entry(row)
        assert entry["@timestamp"] == "2026-03-06T10:00:00Z"
        assert entry["@message"] == "ERROR: fail"
        assert entry["@logStream"] == "stream1"

    def test_missing_message_defaults_empty(self) -> None:
        row = [{"field": "@timestamp", "value": "2026-03-06T10:00:00Z"}]
        entry = _result_row_to_entry(row)
        assert entry["@message"] == ""
        assert entry["@timestamp"] == "2026-03-06T10:00:00Z"


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """validate_config with mocked boto3."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_success_returns_true(
        self, mock_session: MagicMock
    ) -> None:
        client = _mock_logs_client()
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()
        assert collector.validate_config(_aws_config()) is True

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_log_group_not_found_raises(
        self, mock_session: MagicMock
    ) -> None:
        # Paginator returns no matching log group name
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"logGroups": [{"logGroupName": "/other/group"}]},
        ])
        client.get_paginator.return_value = paginator
        mock_session.return_value.client.return_value = client

        collector = CloudWatchCollector()

        with pytest.raises(AWSPermissionError) as exc_info:
            collector.validate_config(_aws_config(log_groups=["/aws/lambda/my-api"]))

        assert "not found" in exc_info.value.message.lower()


# ---------------------------------------------------------------------------
# name property
# ---------------------------------------------------------------------------


class TestCloudWatchCollectorName:
    def test_name_is_cloudwatch(self) -> None:
        collector = CloudWatchCollector()
        assert collector.name == "cloudwatch"
