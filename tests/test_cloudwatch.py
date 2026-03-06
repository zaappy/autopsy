"""Tests for autopsy.collectors.cloudwatch — CloudWatch collector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from autopsy.collectors.cloudwatch import (
    CloudWatchCollector,
    _apply_token_budget,
    _deduplicate,
    _estimate_tokens,
    _template_hash,
    _truncate_entry,
    _truncate_stack_trace,
)
from autopsy.utils.errors import AWSAuthError, AWSPermissionError, NoDataError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aws_config() -> dict:
    """Minimal AWS config for testing."""
    return {
        "region": "us-east-1",
        "log_groups": ["/aws/lambda/my-api"],
        "time_window": 30,
    }


def _make_log_row(timestamp: str, message: str, stream: str = "stream-1") -> list[dict]:
    """Build a Logs Insights result row."""
    return [
        {"field": "@timestamp", "value": timestamp},
        {"field": "@message", "value": message},
        {"field": "@logStream", "value": stream},
        {"field": "@ptr", "value": "ptr-ignore-me"},
    ]


# ---------------------------------------------------------------------------
# Template hashing
# ---------------------------------------------------------------------------


class TestTemplateHash:
    """Message template normalization for dedup."""

    def test_identical_messages_same_hash(self) -> None:
        assert _template_hash("ERROR in handler") == _template_hash("ERROR in handler")

    def test_different_numbers_same_hash(self) -> None:
        h1 = _template_hash("Timeout after 3000ms on request 42")
        h2 = _template_hash("Timeout after 5000ms on request 99")
        assert h1 == h2

    def test_different_timestamps_same_hash(self) -> None:
        h1 = _template_hash("Error at 2026-03-06T10:00:00Z in handler")
        h2 = _template_hash("Error at 2026-03-06T12:30:00Z in handler")
        assert h1 == h2

    def test_different_hex_ids_same_hash(self) -> None:
        h1 = _template_hash("Request abcdef12 failed")
        h2 = _template_hash("Request 12345678 failed")
        assert h1 == h2

    def test_structurally_different_messages(self) -> None:
        h1 = _template_hash("NullPointerException in handler")
        h2 = _template_hash("TimeoutException in scheduler")
        assert h1 != h2


# ---------------------------------------------------------------------------
# Deduplication (Stage 2)
# ---------------------------------------------------------------------------


class TestDeduplicate:
    """Stage 2: hash-based dedup with occurrence counting."""

    def test_unique_messages_kept(self) -> None:
        entries = [
            {"@message": "Error A", "@timestamp": "t1"},
            {"@message": "Error B", "@timestamp": "t2"},
        ]
        result = _deduplicate(entries)
        assert len(result) == 2

    def test_duplicates_collapsed_with_count(self) -> None:
        entries = [
            {"@message": "Timeout after 100ms", "@timestamp": "t1"},
            {"@message": "Timeout after 200ms", "@timestamp": "t2"},
            {"@message": "Timeout after 300ms", "@timestamp": "t3"},
        ]
        result = _deduplicate(entries)
        assert len(result) == 1
        assert result[0]["occurrences"] == 3

    def test_mixed_messages(self) -> None:
        entries = [
            {"@message": "Timeout after 100ms", "@timestamp": "t1"},
            {"@message": "NullPointer in handler", "@timestamp": "t2"},
            {"@message": "Timeout after 200ms", "@timestamp": "t3"},
        ]
        result = _deduplicate(entries)
        assert len(result) == 2
        timeout_entry = next(e for e in result if "Timeout" in e["@message"])
        assert timeout_entry["occurrences"] == 2

    def test_empty_input(self) -> None:
        assert _deduplicate([]) == []


# ---------------------------------------------------------------------------
# Truncation (Stage 3)
# ---------------------------------------------------------------------------


class TestTruncation:
    """Stage 3: per-entry truncation and stack trace trimming."""

    def test_short_message_unchanged(self) -> None:
        entry = {"@message": "short error"}
        result = _truncate_entry(entry)
        assert result["@message"] == "short error"

    def test_long_message_truncated(self) -> None:
        entry = {"@message": "x" * 1000}
        result = _truncate_entry(entry)
        assert len(result["@message"]) == 501  # 500 + ellipsis char

    def test_stack_trace_trimmed(self) -> None:
        frames = "\n".join(
            f"  at com.example.Class{i}.method(Class{i}.java:{i})"
            for i in range(20)
        )
        msg = f"java.lang.NullPointerException\n{frames}"
        result = _truncate_stack_trace(msg)
        at_lines = [line for line in result.split("\n") if line.strip().startswith("at ")]
        assert len(at_lines) == 5

    def test_short_stack_not_trimmed(self) -> None:
        frames = "\n".join(f"  at Class{i}.method()" for i in range(3))
        msg = f"Error\n{frames}"
        result = _truncate_stack_trace(msg)
        assert "omitted" not in result

    def test_original_entry_not_mutated(self) -> None:
        entry = {"@message": "x" * 1000, "@timestamp": "t1"}
        _truncate_entry(entry)
        assert len(entry["@message"]) == 1000


# ---------------------------------------------------------------------------
# Token budget (Stage 4)
# ---------------------------------------------------------------------------


class TestTokenBudget:
    """Stage 4: FIFO eviction to stay within token budget."""

    def test_under_budget_no_eviction(self) -> None:
        entries = [{"@message": "short"}]
        result, truncated = _apply_token_budget(entries)
        assert result == entries
        assert truncated is False

    def test_over_budget_evicts(self) -> None:
        big_entry = {"@message": "x" * 30000}
        entries = [big_entry, big_entry, big_entry]
        result, truncated = _apply_token_budget(entries)
        assert len(result) < len(entries)
        assert truncated is True

    def test_estimate_tokens_basic(self) -> None:
        entries = [{"@message": "a" * 400}]
        tokens = _estimate_tokens(entries)
        assert tokens > 0


# ---------------------------------------------------------------------------
# CloudWatchCollector.validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """Auth and permission validation via mocked boto3."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_valid_credentials(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.describe_log_groups.return_value = {"logGroups": []}
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        assert collector.validate_config(_aws_config()) is True

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_expired_token_raises(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        error_response = {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}}
        mock_client.describe_log_groups.side_effect = ClientError(
            error_response, "DescribeLogGroups"
        )
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        with pytest.raises(AWSAuthError):
            collector.validate_config(_aws_config())

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_access_denied_raises(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        error_response = {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}
        mock_client.describe_log_groups.side_effect = ClientError(
            error_response, "DescribeLogGroups"
        )
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        with pytest.raises(AWSPermissionError):
            collector.validate_config(_aws_config())


# ---------------------------------------------------------------------------
# CloudWatchCollector.collect
# ---------------------------------------------------------------------------


class TestCollect:
    """Full collect() with mocked boto3 calls."""

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_happy_path(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.start_query.return_value = {"queryId": "q-123"}
        mock_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                _make_log_row("2026-03-06T10:00:00Z", "ERROR: NullPointerException in handler"),
                _make_log_row("2026-03-06T10:01:00Z", "ERROR: TimeoutException after 5000ms"),
            ],
        }
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        result = collector.collect(_aws_config())

        assert result.source == "cloudwatch"
        assert result.data_type == "logs"
        assert len(result.entries) == 2
        assert result.entry_count == 2

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_empty_results_raises_no_data(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.start_query.return_value = {"queryId": "q-123"}
        mock_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [],
        }
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        with pytest.raises(NoDataError):
            collector.collect(_aws_config())

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_dedup_applied(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.start_query.return_value = {"queryId": "q-123"}
        mock_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                _make_log_row("t1", "Timeout after 100ms"),
                _make_log_row("t2", "Timeout after 200ms"),
                _make_log_row("t3", "Timeout after 300ms"),
                _make_log_row("t4", "NullPointer in handler"),
            ],
        }
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        result = collector.collect(_aws_config())

        assert result.entry_count == 4
        assert len(result.entries) == 2
        assert result.truncated is True

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_auth_error_on_start_query(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        error_response = {"Error": {"Code": "ExpiredTokenException", "Message": "expired"}}
        mock_client.start_query.side_effect = ClientError(error_response, "StartQuery")
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        with pytest.raises(AWSAuthError):
            collector.collect(_aws_config())

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_profile_passed_to_session(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.start_query.return_value = {"queryId": "q-123"}
        mock_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                _make_log_row("t1", "ERROR: something broke"),
            ],
        }
        mock_session_cls.return_value.client.return_value = mock_client

        config = _aws_config()
        config["profile"] = "my-profile"
        collector = CloudWatchCollector()
        collector.collect(config)

        mock_session_cls.assert_called_with(profile_name="my-profile")

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_multiple_log_groups(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.start_query.return_value = {"queryId": "q-123"}
        mock_client.get_query_results.return_value = {
            "status": "Complete",
            "results": [
                _make_log_row("t1", "ERROR: from group A"),
            ],
        }
        mock_session_cls.return_value.client.return_value = mock_client

        config = _aws_config()
        config["log_groups"] = ["/aws/lambda/api-a", "/aws/lambda/api-b"]
        collector = CloudWatchCollector()
        result = collector.collect(config)

        assert mock_client.start_query.call_count == 2
        assert result.entry_count == 2

    @patch("autopsy.collectors.cloudwatch.boto3.Session")
    def test_failed_query_returns_empty(self, mock_session_cls: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.start_query.return_value = {"queryId": "q-123"}
        mock_client.get_query_results.return_value = {
            "status": "Failed",
            "results": [],
        }
        mock_session_cls.return_value.client.return_value = mock_client

        collector = CloudWatchCollector()
        with pytest.raises(NoDataError):
            collector.collect(_aws_config())
