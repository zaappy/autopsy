"""Tests for autopsy.ai.engine — AI engine, prompt building, response parsing."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from autopsy.ai.engine import (
    AIEngine,
    _extract_json,
    _fallback_result,
    _try_parse,
)
from autopsy.ai.models import DiagnosisResult
from autopsy.ai.prompts import SYSTEM_PROMPT_V1, build_user_prompt
from autopsy.collectors.base import CollectedData
from autopsy.utils.errors import AIAuthError, AIRateLimitError, AITimeoutError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_json_response() -> str:
    """Return a well-formed DiagnosisResult JSON string."""
    return json.dumps(
        {
            "root_cause": {
                "summary": "NullPointerException from missing null check",
                "category": "code_change",
                "confidence": 0.9,
                "evidence": ["ERROR: NullPointerException in handler"],
            },
            "correlated_deploy": {
                "commit_sha": "abc1234",
                "author": "dev",
                "pr_title": "fix handler",
                "changed_files": ["src/handler.py"],
            },
            "suggested_fix": {
                "immediate": "Revert commit abc1234",
                "long_term": "Add null guards and unit tests",
            },
            "timeline": [
                {"time": "2026-03-06T10:00:00Z", "event": "Commit merged"},
                {"time": "2026-03-06T10:05:00Z", "event": "First error"},
            ],
        }
    )


def _malformed_response() -> str:
    """Return a response that is not valid JSON."""
    return "Here is my analysis:\nThe root cause is a missing null check..."


def _logs_data() -> CollectedData:
    """Minimal logs CollectedData for prompt tests."""
    now = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)
    return CollectedData(
        source="cloudwatch",
        data_type="logs",
        entries=[
            {
                "@timestamp": "2026-03-06T10:00:00Z",
                "@message": "ERROR: NullPointerException in handler",
                "occurrences": 3,
            },
        ],
        time_range=(now, now),
        raw_query="fields @timestamp | filter error",
        entry_count=5,
        truncated=True,
    )


def _deploys_data() -> CollectedData:
    """Minimal deploys CollectedData for prompt tests."""
    now = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)
    return CollectedData(
        source="github",
        data_type="deploys",
        entries=[
            {
                "sha": "abc1234def5678",
                "author": "dev",
                "timestamp": "2026-03-06T09:55:00Z",
                "message": "fix: null check\ndetailed body here",
                "files": ["src/handler.py"],
                "file_diffs": [
                    {
                        "filename": "src/handler.py",
                        "status": "modified",
                        "additions": 5,
                        "deletions": 2,
                        "diff": "+if obj is None:\n+    raise ValueError",
                    }
                ],
            },
        ],
        time_range=(now, now),
        raw_query="last 5 commits on main",
        entry_count=1,
        truncated=False,
    )


def _make_engine(chat_side_effect: list | None = None) -> tuple[AIEngine, MagicMock]:
    """Build an AIEngine with a mocked provider, bypassing real SDK init.

    Args:
        chat_side_effect: Optional side_effect list for the mock's chat method.

    Returns:
        (engine, mock_provider) tuple.
    """
    mock_provider = MagicMock()
    if chat_side_effect is not None:
        mock_provider.chat.side_effect = chat_side_effect
    else:
        mock_provider.chat.return_value = _valid_json_response()

    mock_cls = MagicMock(return_value=mock_provider)
    with patch.dict("autopsy.ai.engine._PROVIDERS", {"anthropic": mock_cls, "openai": mock_cls}):
        engine = AIEngine("anthropic", "claude-sonnet-4-20250514", "sk-test")
    return engine, mock_provider


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


class TestExtractJson:
    """Strip markdown fences and surrounding text from JSON."""

    def test_plain_json(self) -> None:
        raw = '{"key": "value"}'
        assert _extract_json(raw) == raw

    def test_markdown_fenced(self) -> None:
        raw = '```json\n{"key": "value"}\n```'
        assert _extract_json(raw) == '{"key": "value"}'

    def test_surrounding_text(self) -> None:
        raw = 'Here is the result:\n{"key": "value"}\nDone.'
        assert _extract_json(raw) == '{"key": "value"}'

    def test_no_json_returns_original(self) -> None:
        raw = "no json here"
        assert _extract_json(raw) == "no json here"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestTryParse:
    """Parse raw JSON into DiagnosisResult."""

    def test_valid_json(self) -> None:
        result = _try_parse(_valid_json_response())
        assert result is not None
        assert result.root_cause.category == "code_change"
        assert result.root_cause.confidence == 0.9

    def test_malformed_returns_none(self) -> None:
        assert _try_parse(_malformed_response()) is None

    def test_partial_json_returns_none(self) -> None:
        assert _try_parse('{"root_cause": {}}') is None

    def test_fenced_json_parsed(self) -> None:
        fenced = f"```json\n{_valid_json_response()}\n```"
        result = _try_parse(fenced)
        assert result is not None
        assert result.root_cause.summary == "NullPointerException from missing null check"


# ---------------------------------------------------------------------------
# Fallback result
# ---------------------------------------------------------------------------


class TestFallbackResult:
    """Fallback when parsing fails entirely."""

    def test_raw_response_preserved(self) -> None:
        result = _fallback_result("some raw text")
        assert result.raw_response == "some raw text"
        assert result.root_cause.category == "unknown"
        assert result.root_cause.confidence == 0.0


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    """Assemble collected data into a user prompt."""

    def test_logs_section(self) -> None:
        prompt = build_user_prompt([_logs_data()])
        assert "ERROR LOGS" in prompt
        assert "NullPointerException" in prompt
        assert "×3" in prompt

    def test_deploys_section(self) -> None:
        prompt = build_user_prompt([_deploys_data()])
        assert "RECENT DEPLOYS" in prompt
        assert "abc1234d" in prompt
        assert "src/handler.py" in prompt

    def test_combined(self) -> None:
        prompt = build_user_prompt([_logs_data(), _deploys_data()])
        assert "ERROR LOGS" in prompt
        assert "RECENT DEPLOYS" in prompt
        assert "CROSS-SOURCE ANALYSIS INSTRUCTIONS" in prompt

    def test_empty_data(self) -> None:
        prompt = build_user_prompt([])
        assert "No data was collected" in prompt

    def test_truncation_noted(self) -> None:
        prompt = build_user_prompt([_logs_data()])
        assert "truncated" in prompt


# ---------------------------------------------------------------------------
# AIEngine initialization
# ---------------------------------------------------------------------------


class TestAIEngineInit:
    """Engine constructor and provider wiring."""

    def test_anthropic_provider_created(self) -> None:
        mock_cls = MagicMock()
        with patch.dict("autopsy.ai.engine._PROVIDERS", {"anthropic": mock_cls}):
            engine = AIEngine("anthropic", "claude-sonnet-4-20250514", "sk-test")
        assert engine.provider_name == "anthropic"
        mock_cls.assert_called_once_with("sk-test")

    def test_openai_provider_created(self) -> None:
        mock_cls = MagicMock()
        with patch.dict("autopsy.ai.engine._PROVIDERS", {"openai": mock_cls}):
            engine = AIEngine("openai", "gpt-4o", "sk-test")
        assert engine.provider_name == "openai"
        mock_cls.assert_called_once_with("sk-test")

    def test_unknown_provider_raises(self) -> None:
        with (
            pytest.raises(ValueError, match="Unsupported AI provider"),
            patch.dict("autopsy.ai.engine._PROVIDERS", clear=True),
        ):
            AIEngine("gemini", "gemini-pro", "key")


# ---------------------------------------------------------------------------
# AIEngine.diagnose — happy path
# ---------------------------------------------------------------------------


class TestDiagnoseHappyPath:
    """LLM returns valid JSON on first call."""

    def test_returns_parsed_result(self) -> None:
        engine, mock_prov = _make_engine()
        result = engine.diagnose([_logs_data(), _deploys_data()])

        assert isinstance(result, DiagnosisResult)
        assert result.root_cause.category == "code_change"
        assert result.raw_response is None
        mock_prov.chat.assert_called_once()

    def test_system_prompt_is_v1(self) -> None:
        engine, mock_prov = _make_engine()
        engine.diagnose([_logs_data()])

        call_args = mock_prov.chat.call_args
        assert call_args[0][0] == SYSTEM_PROMPT_V1


# ---------------------------------------------------------------------------
# AIEngine.diagnose — retry on malformed JSON
# ---------------------------------------------------------------------------


class TestDiagnoseRetry:
    """LLM returns malformed JSON → retry with correction → success."""

    def test_retry_succeeds(self) -> None:
        engine, mock_prov = _make_engine(
            [
                _malformed_response(),
                _valid_json_response(),
            ]
        )
        result = engine.diagnose([_logs_data()])

        assert result.root_cause.category == "code_change"
        assert result.raw_response is None
        assert mock_prov.chat.call_count == 2

    def test_retry_includes_correction_prompt(self) -> None:
        engine, mock_prov = _make_engine(
            [
                _malformed_response(),
                _valid_json_response(),
            ]
        )
        engine.diagnose([_logs_data()])

        retry_call = mock_prov.chat.call_args_list[1]
        messages = retry_call[0][1]
        assert len(messages) == 3
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"
        assert "not valid JSON" in messages[2]["content"]


# ---------------------------------------------------------------------------
# AIEngine.diagnose — fallback to raw
# ---------------------------------------------------------------------------


class TestDiagnoseFallback:
    """LLM returns malformed JSON twice → fallback to raw."""

    def test_fallback_to_raw(self) -> None:
        engine, mock_prov = _make_engine(
            [
                _malformed_response(),
                "still not json",
            ]
        )
        result = engine.diagnose([_logs_data()])

        assert result.raw_response == "still not json"
        assert result.root_cause.category == "unknown"
        assert result.root_cause.confidence == 0.0
        assert mock_prov.chat.call_count == 2


# ---------------------------------------------------------------------------
# AIEngine.diagnose — error handling
# ---------------------------------------------------------------------------


class TestMultiSourcePromptBuilding:
    """Verify build_user_prompt groups and labels data by source."""

    @staticmethod
    def _make_logs(source: str, entries: list[dict] | None = None,
                   *, truncated: bool = False, entry_count: int = 5) -> CollectedData:
        now = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)
        return CollectedData(
            source=source,
            data_type="logs",
            entries=entries or [
                {"@timestamp": "2026-03-06T10:00:00Z",
                 "@message": f"ERROR from {source}", "occurrences": 1},
            ],
            time_range=(now, now),
            raw_query=f"query for {source}",
            entry_count=entry_count,
            truncated=truncated,
        )

    @staticmethod
    def _make_deploys(source: str, entries: list[dict] | None = None) -> CollectedData:
        now = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)
        return CollectedData(
            source=source,
            data_type="deploys",
            entries=entries or [
                {"sha": "abc1234def5678", "author": "dev", "timestamp": "2026-03-06T09:55:00Z",
                 "message": f"commit from {source}"},
            ],
            time_range=(now, now),
            raw_query=f"last 5 commits on {source}",
            entry_count=1,
            truncated=False,
        )

    def test_build_prompt_single_log_source(self) -> None:
        prompt = build_user_prompt([self._make_logs("cloudwatch")])
        assert "=== ERROR LOGS ===" in prompt
        assert "--- Source: Cloudwatch ---" in prompt
        assert "CROSS-SOURCE ANALYSIS" not in prompt  # single collector

    def test_build_prompt_logs_plus_deploy_cross_source(self) -> None:
        """CloudWatch + GitHub (one log + one deploy): correlate across systems."""
        prompt = build_user_prompt([
            self._make_logs("cloudwatch"),
            self._make_deploys("github"),
        ])
        assert "CROSS-SOURCE ANALYSIS INSTRUCTIONS" in prompt
        assert "cloudwatch, github" in prompt

    def test_build_prompt_deploy_files_fallback(self) -> None:
        """Deploy entry with bare files list (no diffs) still lists files."""
        now = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)
        deploys = CollectedData(
            source="github",
            data_type="deploys",
            entries=[
                {
                    "sha": "abc1234def5678",
                    "author": "dev",
                    "timestamp": "2026-03-06T09:55:00Z",
                    "message": "fix",
                    "files": ["src/Foo.java", "src/Bar.java"],
                },
            ],
            time_range=(now, now),
            raw_query="main",
            entry_count=1,
            truncated=False,
        )
        prompt = build_user_prompt([deploys])
        assert "Files: src/Foo.java, src/Bar.java" in prompt

    def test_build_prompt_multi_log_sources(self) -> None:
        prompt = build_user_prompt([
            self._make_logs("cloudwatch"),
            self._make_logs("datadog"),
        ])
        assert "--- Source: Cloudwatch ---" in prompt
        assert "--- Source: Datadog ---" in prompt
        assert "CROSS-SOURCE ANALYSIS INSTRUCTIONS" in prompt

    def test_build_prompt_multi_deploy_sources(self) -> None:
        prompt = build_user_prompt([
            self._make_deploys("github"),
            self._make_deploys("gitlab"),
        ])
        assert "--- Source: Github ---" in prompt
        assert "--- Source: Gitlab ---" in prompt
        assert "CROSS-SOURCE ANALYSIS INSTRUCTIONS" in prompt

    def test_build_prompt_no_logs(self) -> None:
        prompt = build_user_prompt([self._make_deploys("github")])
        assert "No log data available" in prompt

    def test_build_prompt_no_deploys(self) -> None:
        prompt = build_user_prompt([self._make_logs("cloudwatch")])
        assert "No deployment data available" in prompt

    def test_build_prompt_full_multi(self) -> None:
        prompt = build_user_prompt([
            self._make_logs("cloudwatch"),
            self._make_logs("datadog"),
            self._make_deploys("github"),
            self._make_deploys("gitlab"),
        ])
        assert "--- Source: Cloudwatch ---" in prompt
        assert "--- Source: Datadog ---" in prompt
        assert "--- Source: Github ---" in prompt
        assert "--- Source: Gitlab ---" in prompt
        assert "CROSS-SOURCE ANALYSIS INSTRUCTIONS" in prompt
        assert "cloudwatch, datadog, github, gitlab" in prompt

    def test_build_prompt_entries_labeled(self) -> None:
        logs = self._make_logs("cloudwatch", entries=[
            {"@timestamp": "2026-03-06T10:00:00Z", "@message": "NPE in handler",
             "log_level": "ERROR", "occurrences": 1},
        ])
        deploys = self._make_deploys("github", entries=[
            {"sha": "abc1234def5678", "author": "sarah",
             "timestamp": "2026-03-06T09:55:00Z",
             "message": "fix payment", "diffs": [
                 {"new_path": "PaymentService.java", "diff": "+null check"}]},
        ])
        prompt = build_user_prompt([logs, deploys])
        assert "[2026-03-06T10:00:00Z] [ERROR] NPE in handler" in prompt
        assert "abc1234d" in prompt
        assert "@sarah" in prompt
        assert "PaymentService.java" in prompt

    def test_build_prompt_truncation_note(self) -> None:
        prompt = build_user_prompt([self._make_logs("cloudwatch", truncated=True)])
        assert "\u26a0 Data was truncated" in prompt

    def test_build_prompt_occurrences(self) -> None:
        logs = self._make_logs("cloudwatch", entries=[
            {"@timestamp": "2026-03-06T10:00:00Z", "@message": "NPE",
             "occurrences": 5},
        ])
        prompt = build_user_prompt([logs])
        assert "(\u00d75)" in prompt


class TestDiagnoseErrors:
    """Auth, rate limit, and timeout errors bubble up correctly."""

    def test_auth_error(self) -> None:
        engine, _ = _make_engine(
            [
                AIAuthError(message="API key is invalid.", hint="Check key."),
            ]
        )
        with pytest.raises(AIAuthError, match="invalid"):
            engine.diagnose([_logs_data()])

    def test_rate_limit_error(self) -> None:
        engine, _ = _make_engine(
            [
                AIRateLimitError(message="Rate limit exceeded.", hint="Wait."),
            ]
        )
        with pytest.raises(AIRateLimitError, match="Rate limit"):
            engine.diagnose([_logs_data()])

    def test_timeout_error(self) -> None:
        engine, _ = _make_engine(
            [
                AITimeoutError(message="API timed out after 60s.", hint="Retry."),
            ]
        )
        with pytest.raises(AITimeoutError, match="timed out"):
            engine.diagnose([_logs_data()])

    def test_auth_error_on_retry_still_raises(self) -> None:
        """First call returns malformed JSON, retry raises auth error."""
        engine, _ = _make_engine(
            [
                _malformed_response(),
                AIAuthError(message="Token revoked.", hint="Re-create key."),
            ]
        )
        with pytest.raises(AIAuthError, match="revoked"):
            engine.diagnose([_logs_data()])
