from __future__ import annotations

"""Tests for autopsy.utils.log_reduction — shared log reduction pipeline."""

from autopsy.utils.log_reduction import (
    apply_token_budget,
    deduplicate_logs,
    extract_template,
    truncate_entries,
)


class TestExtractTemplate:
    def test_uuids_replaced(self) -> None:
        msg = "Request 550e8400-e29b-41d4-a716-446655440000 failed"
        out = extract_template(msg)
        assert "<UUID>" in out
        assert "550e8400" not in out

    def test_ips_replaced(self) -> None:
        msg = "Connection from 10.0.0.1 failed"
        out = extract_template(msg)
        assert "<IP>" in out
        assert "10.0.0.1" not in out

    def test_timestamps_replaced(self) -> None:
        msg = "At 2026-03-06T10:00:00Z we saw an error"
        out = extract_template(msg)
        assert "<TS>" in out
        assert "2026-03-06" not in out

    def test_numbers_replaced(self) -> None:
        msg = "Order 123456 failed with code 500"
        out = extract_template(msg)
        assert "<NUM>" in out
        assert "123456" not in out

    def test_hex_replaced(self) -> None:
        msg = "Hash deadbeefcafebabe is invalid"
        out = extract_template(msg)
        assert "<HEX>" in out
        assert "deadbeef" not in out


class TestDeduplicateLogs:
    def test_counts_occurrences_and_keeps_first(self) -> None:
        entries = [
            {"message": "Error for id 12345", "id": 1},
            {"message": "Error for id 67890", "id": 2},
            {"message": "Error for id 99999", "id": 3},
        ]
        # Same template after number normalization
        result = deduplicate_logs(entries, message_key="message")
        assert len(result) == 1
        only = result[0]
        assert only["occurrences"] == 3
        assert only["id"] == 1  # first instance is kept


class TestTruncateEntries:
    def test_preserves_short_messages(self) -> None:
        entries = [{"message": "short"}]
        out = truncate_entries(entries)
        assert out[0]["message"] == "short"

    def test_truncates_long_message(self) -> None:
        msg = "x" * 1000
        entries = [{"message": msg}]
        out = truncate_entries(entries)
        assert len(out[0]["message"]) <= 501
        assert out[0]["message"].endswith("…")

    def test_trims_stack_traces(self) -> None:
        lines = [
            "Traceback (most recent call last):",
            '  File "/app/main.py", line 1, in <module>',
            '  File "/app/main.py", line 2, in <module>',
            '  File "/app/main.py", line 3, in <module>',
            '  File "/app/main.py", line 4, in <module>',
            '  File "/app/main.py", line 5, in <module>',
            '  File "/app/main.py", line 6, in <module>',
        ]
        msg = "\n".join(lines)
        entries = [{"message": msg}]
        out = truncate_entries(entries)
        value = out[0]["message"]
        assert value.startswith("Traceback")
        assert value.count("  File ") <= 5


class TestApplyTokenBudget:
    def test_fifo_eviction_under_budget_flag(self) -> None:
        # Build entries newest-first so helper's FIFO eviction removes oldest.
        entries = [
            {"message": "a" * 400, "timestamp": f"2026-03-06T10:00:{i:02d}Z"}
            for i in range(99, -1, -1)
        ]
        kept, truncated = apply_token_budget(entries, budget=3000)
        assert truncated is True
        assert len(kept) < len(entries)
        # Oldest entries should be evicted; entries are newest-first into budget helper
        timestamps = [e["timestamp"] for e in kept]
        assert timestamps == sorted(timestamps)

    def test_no_eviction_when_under_budget(self) -> None:
        entries = [
            {"message": "short", "timestamp": "2026-03-06T10:00:00Z"},
            {"message": "short2", "timestamp": "2026-03-06T10:05:00Z"},
        ]
        kept, truncated = apply_token_budget(entries, budget=6000)
        assert truncated is False
        assert kept == entries

