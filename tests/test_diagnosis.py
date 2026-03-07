"""Tests for autopsy.diagnosis — orchestrator integration tests.

Integration tests with fully mocked collectors and AI engine.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from autopsy.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SuggestedFix,
    TimelineEvent,
)
from autopsy.config import AIConfig, AutopsyConfig, AWSConfig, GitHubConfig, OutputConfig
from autopsy.diagnosis import DiagnosisOrchestrator
from autopsy.renderers.json_out import JSONRenderer
from autopsy.renderers.terminal import TerminalRenderer


def _minimal_config() -> AutopsyConfig:
    """Build minimal AutopsyConfig for tests."""
    return AutopsyConfig(
        aws=AWSConfig(
            region="us-east-1",
            log_groups=["/aws/lambda/test"],
            time_window=30,
        ),
        github=GitHubConfig(
            repo="owner/repo",
            token_env="GITHUB_TOKEN",
            deploy_count=5,
            branch="main",
        ),
        ai=AIConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key_env="ANTHROPIC_API_KEY",
        ),
        output=OutputConfig(),
    )


def _sample_result() -> DiagnosisResult:
    """Sample DiagnosisResult for assertions."""
    return DiagnosisResult(
        root_cause=RootCause(
            summary="Test root cause",
            category="code_change",
            confidence=0.9,
            evidence=["line 1", "line 2"],
        ),
        correlated_deploy=CorrelatedDeploy(
            commit_sha="abc123",
            author="dev",
            pr_title="fix: thing",
            changed_files=["src/foo.py"],
        ),
        suggested_fix=SuggestedFix(
            immediate="Revert abc123",
            long_term="Add tests",
        ),
        timeline=[
            TimelineEvent(time="2026-03-06T10:00:00Z", event="Deploy"),
            TimelineEvent(time="2026-03-06T10:05:00Z", event="Error"),
        ],
    )


# ---------------------------------------------------------------------------
# DiagnosisOrchestrator.run — mocked collectors + AI
# ---------------------------------------------------------------------------


class TestOrchestratorRun:
    """Orchestrator run() with mocked CloudWatch, GitHub, and AI."""

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_run_returns_ai_result(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock(source="cloudwatch", data_type="logs")
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(source="github", data_type="deploys")
        mock_gh_cls.return_value = mock_gh

        expected = _sample_result()
        mock_engine = MagicMock()
        mock_engine.diagnose.return_value = expected
        mock_engine_cls.return_value = mock_engine

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        result = orch.run()

        assert result.root_cause.summary == "Test root cause"
        assert result.root_cause.confidence == 0.9
        mock_cw.validate_config.assert_called_once()
        mock_cw.collect.assert_called_once()
        mock_gh.validate_config.assert_called_once()
        mock_gh.collect.assert_called_once()
        mock_engine.diagnose.assert_called_once()
        call_args = mock_engine.diagnose.call_args[0][0]
        assert len(call_args) == 2

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_run_passes_overrides_to_collect(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock()
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock()
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        orch.run(time_window=15, log_groups=["/custom/group"], provider="openai")

        cw_collect_call = mock_cw.collect.call_args[0][0]
        assert cw_collect_call["time_window"] == 15
        assert cw_collect_call["log_groups"] == ["/custom/group"]
        mock_engine_cls.assert_called_once()
        assert mock_engine_cls.call_args[1]["provider"] == "openai"

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_run_raises_when_ai_key_missing(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)

        from autopsy.utils.errors import AIAuthError

        with pytest.raises(AIAuthError, match="not set"):
            orch.run()


# ---------------------------------------------------------------------------
# Renderers (smoke tests with sample_diagnosis_result)
# ---------------------------------------------------------------------------


class TestTerminalRenderer:
    """Terminal renderer produces output without error."""

    def test_render_does_not_raise(self, sample_diagnosis_result: DiagnosisResult) -> None:
        r = TerminalRenderer()
        r.render(sample_diagnosis_result)

    def test_render_with_raw_response(self, sample_diagnosis_result: DiagnosisResult) -> None:
        sample_diagnosis_result.raw_response = "Raw fallback text"
        r = TerminalRenderer()
        r.render(sample_diagnosis_result)


class TestJSONRenderer:
    """JSON renderer outputs valid JSON."""

    def test_render_outputs_valid_json(
        self, sample_diagnosis_result: DiagnosisResult, capsys: pytest.CaptureFixture
    ) -> None:
        r = JSONRenderer()
        r.render(sample_diagnosis_result)
        out, _ = capsys.readouterr()
        assert '"root_cause"' in out
        assert '"summary"' in out
        assert '"correlated_deploy"' in out
        assert "\n  " in out


# ---------------------------------------------------------------------------
# CLI diagnose — integration
# ---------------------------------------------------------------------------


class TestCLIDiagnose:
    """CLI diagnose command with mocked pipeline."""

    @patch("autopsy.diagnosis.DiagnosisOrchestrator")
    @patch("autopsy.config.load_config")
    def test_diagnose_calls_orchestrator_and_renderer(
        self,
        mock_load_config: MagicMock,
        mock_orch_cls: MagicMock,
    ) -> None:
        from click.testing import CliRunner

        from autopsy.cli import cli

        mock_load_config.return_value = _minimal_config()
        mock_orch = MagicMock()
        mock_orch.run.return_value = _sample_result()
        mock_orch_cls.return_value = mock_orch

        runner = CliRunner()
        result = runner.invoke(cli, ["diagnose"], catch_exceptions=False)

        assert result.exit_code == 0
        mock_orch.run.assert_called_once()
        assert "Root Cause" in result.output or "root_cause" in result.output

    @patch("autopsy.diagnosis.DiagnosisOrchestrator")
    @patch("autopsy.config.load_config")
    def test_diagnose_json_flag_outputs_json(
        self,
        mock_load_config: MagicMock,
        mock_orch_cls: MagicMock,
    ) -> None:
        from click.testing import CliRunner

        from autopsy.cli import cli

        mock_load_config.return_value = _minimal_config()
        mock_orch = MagicMock()
        mock_orch.run.return_value = _sample_result()
        mock_orch_cls.return_value = mock_orch

        runner = CliRunner()
        result = runner.invoke(cli, ["diagnose", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        assert '"root_cause"' in result.output
        assert '"summary"' in result.output
