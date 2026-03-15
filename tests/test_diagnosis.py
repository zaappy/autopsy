"""Tests for autopsy.diagnosis — orchestrator integration tests.

Integration tests with fully mocked collectors and AI engine.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from autopsy.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SourceInfo,
    SuggestedFix,
    TimelineEvent,
)
from autopsy.config import (
    AIConfig,
    AutopsyConfig,
    AWSConfig,
    DatadogConfig,
    GitHubConfig,
    OutputConfig,
)
from autopsy.diagnosis import DiagnosisOrchestrator, _CollectorTask
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
        mock_cw.collect.return_value = MagicMock(source="cloudwatch", data_type="logs", entries=[])
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(source="github", data_type="deploys", entries=[])
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")  # provider=openai requires this

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

        with pytest.raises(AIAuthError, match="not found"):
            orch.run()

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_run_raises_clear_error_for_provider_override_missing_key(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--provider openai with missing OpenAI key shows clear error."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)

        from autopsy.utils.errors import AIAuthError

        with pytest.raises(AIAuthError, match="OpenAI API key not found"):
            orch.run(provider="openai")

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_run_skips_datadog_when_keys_missing(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Datadog configured but DD_API_KEY/DD_APP_KEY unset: warn and skip, CW + GH still run."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
        monkeypatch.delenv("DD_API_KEY", raising=False)
        monkeypatch.delenv("DD_APP_KEY", raising=False)

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock(source="cloudwatch", data_type="logs")
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(source="github", data_type="deploys")
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = AutopsyConfig(
            aws=AWSConfig(
                region="us-east-1",
                log_groups=["/aws/lambda/test"],
                time_window=30,
            ),
            datadog=DatadogConfig(site="us1", time_window=30),
            github=GitHubConfig(
                repo="owner/repo",
                token_env="GITHUB_TOKEN",
                deploy_count=5,
                branch="main",
            ),
            ai=AIConfig(
                provider="anthropic",
                model="claude-sonnet-4-20250514",
            ),
            output=OutputConfig(),
        )
        orch = DiagnosisOrchestrator(config)
        result = orch.run()

        assert result.root_cause.summary == "Test root cause"
        mock_cw.validate_config.assert_called_once()
        mock_cw.collect.assert_called_once()
        mock_gh.validate_config.assert_called_once()
        mock_gh.collect.assert_called_once()
        call_args = mock_engine_cls.return_value.diagnose.call_args[0][0]
        assert len(call_args) == 2, "Datadog skipped; only CloudWatch + GitHub collected"


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

    def test_sources_panel_multi(self, sample_diagnosis_result: DiagnosisResult) -> None:
        """Sources panel when multiple log sources (or multiple deploy)."""
        sample_diagnosis_result.sources = [
            SourceInfo(name="cloudwatch", data_type="logs", entry_count=23),
            SourceInfo(name="datadog", data_type="logs", entry_count=15),
            SourceInfo(name="github", data_type="deploys", entry_count=5),
        ]
        r = TerminalRenderer()
        renderables = r.get_renderables(sample_diagnosis_result)
        from rich.panel import Panel

        first = renderables[0]
        assert isinstance(first, Panel)
        assert first.title is not None
        # Panel title should contain "SOURCES"
        from rich.text import Text

        title_text = first.title.plain if isinstance(first.title, Text) else str(first.title)
        assert "SOURCES" in title_text

    def test_sources_panel_single(self, sample_diagnosis_result: DiagnosisResult) -> None:
        """No sources panel when only 1 source."""
        sample_diagnosis_result.sources = [
            SourceInfo(name="cloudwatch", data_type="logs", entry_count=23),
        ]
        r = TerminalRenderer()
        renderables = r.get_renderables(sample_diagnosis_result)
        from rich.panel import Panel
        from rich.text import Text

        for renderable in renderables:
            if isinstance(renderable, Panel) and renderable.title:
                t = renderable.title
                title_text = t.plain if isinstance(t, Text) else str(t)
                assert "SOURCES" not in title_text

    def test_sources_panel_empty(self, sample_diagnosis_result: DiagnosisResult) -> None:
        """No sources panel when 0 sources."""
        sample_diagnosis_result.sources = []
        r = TerminalRenderer()
        renderables = r.get_renderables(sample_diagnosis_result)
        from rich.panel import Panel
        from rich.text import Text

        for renderable in renderables:
            if isinstance(renderable, Panel) and renderable.title:
                t = renderable.title
                title_text = t.plain if isinstance(t, Text) else str(t)
                assert "SOURCES" not in title_text

    def test_sources_panel_cw_github_no_panel(
        self, sample_diagnosis_result: DiagnosisResult
    ) -> None:
        """Classic CloudWatch + GitHub: no SOURCES panel (backward compatible)."""
        sample_diagnosis_result.sources = [
            SourceInfo(name="cloudwatch", data_type="logs", entry_count=10),
            SourceInfo(name="github", data_type="deploys", entry_count=3),
        ]
        r = TerminalRenderer()
        renderables = r.get_renderables(sample_diagnosis_result)
        from rich.panel import Panel
        from rich.text import Text

        for renderable in renderables:
            if isinstance(renderable, Panel) and renderable.title:
                t = renderable.title
                title_text = t.plain if isinstance(t, Text) else str(t)
                assert "SOURCES" not in title_text


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
        assert "confidence_level" in out
        assert "\n  " in out

    def test_json_output_includes_sources(
        self, sample_diagnosis_result: DiagnosisResult, capsys: pytest.CaptureFixture
    ) -> None:
        """JSON output includes sources array when populated."""
        import json

        sample_diagnosis_result.sources = [
            SourceInfo(name="cloudwatch", data_type="logs", entry_count=23),
            SourceInfo(name="github", data_type="deploys", entry_count=5),
        ]
        r = JSONRenderer()
        r.render(sample_diagnosis_result)
        out, _ = capsys.readouterr()
        data = json.loads(out)
        assert "sources" in data
        assert len(data["sources"]) == 2
        assert data["sources"][0]["name"] == "cloudwatch"
        assert data["sources"][1]["entry_count"] == 5


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


# ---------------------------------------------------------------------------
# Parallel / sequential collection
# ---------------------------------------------------------------------------


def _make_mock_task(
    role: str,
    *,
    delay: float = 0.0,
    fail: bool = False,
    error_cls: type = RuntimeError,
) -> _CollectorTask:
    """Create a _CollectorTask with a mock collector.

    Args:
        role: Collector role name.
        delay: Simulated collection time in seconds.
        fail: If True, collect() raises an exception.
        error_cls: Exception class to raise on failure.
    """
    collector = MagicMock()
    collector.validate_config.return_value = True

    def _collect(cfg: dict):
        if delay:
            time.sleep(delay)
        if fail:
            raise error_cls(f"{role} boom")
        mock_data = MagicMock()
        mock_data.source = role
        mock_data.data_type = "logs"
        return mock_data

    collector.collect.side_effect = _collect
    return _CollectorTask(collector=collector, config={}, role=role)


class TestParallelCollection:
    """Tests for parallel and sequential collector execution."""

    # 1. Parallel is faster than sequential for multiple slow collectors
    def test_parallel_faster_than_sequential(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw", delay=0.15),
            _make_mock_task("gh", delay=0.15),
        ]
        t0 = time.monotonic()
        collected, timings, errors = orch._collect_parallel(tasks)
        parallel_time = time.monotonic() - t0

        assert len(collected) == 2
        assert not errors
        # Parallel should finish in less than the sum of delays
        assert parallel_time < 0.25, (
            f"Parallel took {parallel_time:.2f}s, expected < 0.25s"
        )

    # 2. Sequential flag forces serial execution
    def test_sequential_runs_serially(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw", delay=0.1),
            _make_mock_task("gh", delay=0.1),
        ]
        t0 = time.monotonic()
        collected, timings, errors = orch._collect_sequential(tasks)
        seq_time = time.monotonic() - t0

        assert len(collected) == 2
        assert not errors
        # Sequential should take at least the sum of delays
        assert seq_time >= 0.18, (
            f"Sequential took {seq_time:.2f}s, expected >= 0.18s"
        )

    # 3. Partial failure: one collector fails, others succeed
    def test_partial_failure_returns_successful(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw"),
            _make_mock_task("gh", fail=True),
        ]
        collected, timings, errors = orch._collect_parallel(tasks)

        assert len(collected) == 1
        assert collected[0].source == "cw"
        assert len(errors) == 1
        assert "gh" in errors[0]

    # 4. All collectors fail — raises CollectorError
    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    @patch("autopsy.diagnosis.GitHubCollector")
    def test_all_fail_raises_collector_error(
        self,
        mock_gh_cls: MagicMock,
        mock_cw_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.side_effect = RuntimeError("cw boom")
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.side_effect = RuntimeError("gh boom")
        mock_gh_cls.return_value = mock_gh

        from autopsy.utils.errors import CollectorError

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        with pytest.raises(CollectorError, match="All collectors failed"):
            orch.run()

    # 5. Timings dict populated for each collector
    def test_timings_populated(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw", delay=0.05),
            _make_mock_task("gh"),
        ]
        _, timings, _ = orch._collect_parallel(tasks)

        assert "cw" in timings
        assert "gh" in timings
        assert timings["cw"] >= 0.04

    # 6. Single collector skips parallel, uses sequential
    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_single_collector_uses_sequential(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With only one collector, run() uses sequential path."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock(
            source="cloudwatch", data_type="logs", entries=[]
        )
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(
            source="github", data_type="deploys", entries=[]
        )
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)

        # Patch to verify sequential path is used
        with patch.object(
            orch, "_collect_sequential", wraps=orch._collect_sequential
        ) as spy_seq, patch.object(
            orch, "_collect_parallel", wraps=orch._collect_parallel
        ) as spy_par:
            # Force only one task via sequential flag
            orch.run(sequential=True)
            spy_seq.assert_called_once()
            spy_par.assert_not_called()

    # 7. _safe_collect catches exceptions
    def test_safe_collect_catches_errors(self) -> None:
        task = _make_mock_task("cw", fail=True)
        data, elapsed, err = DiagnosisOrchestrator._safe_collect(task)

        assert data is None
        assert err is not None
        assert "cw" in err
        assert elapsed >= 0

    # 8. _safe_collect returns data on success
    def test_safe_collect_returns_data(self) -> None:
        task = _make_mock_task("gh")
        data, elapsed, err = DiagnosisOrchestrator._safe_collect(task)

        assert data is not None
        assert data.source == "gh"
        assert err is None
        assert elapsed >= 0

    # 9. Threaded fallback works
    def test_threaded_fallback(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw"),
            _make_mock_task("gh"),
        ]
        collected, timings, errors = orch._collect_threaded(tasks)

        assert len(collected) == 2
        assert not errors
        assert "cw" in timings
        assert "gh" in timings

    # 10. run() with sequential=True uses sequential path
    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_run_sequential_flag(
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
        mock_cw.collect.return_value = MagicMock(
            source="cloudwatch", data_type="logs"
        )
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(
            source="github", data_type="deploys"
        )
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)

        with patch.object(
            orch, "_collect_sequential", wraps=orch._collect_sequential
        ) as spy:
            result = orch.run(sequential=True)
            spy.assert_called_once()

        assert result.root_cause.summary == "Test root cause"

    # 11. Parallel execution still returns correct AI result
    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_parallel_returns_correct_result(
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
        mock_cw.collect.return_value = MagicMock(
            source="cloudwatch", data_type="logs"
        )
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(
            source="github", data_type="deploys"
        )
        mock_gh_cls.return_value = mock_gh

        expected = _sample_result()
        mock_engine_cls.return_value.diagnose.return_value = expected

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        result = orch.run(sequential=False)

        assert result.root_cause.summary == "Test root cause"
        assert result.root_cause.confidence == 0.9
        call_args = mock_engine_cls.return_value.diagnose.call_args[0][0]
        assert len(call_args) == 2

    # 12. Unexpected error (ValueError) — others still succeed
    def test_parallel_unexpected_error(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw"),
            _make_mock_task("gh", fail=True, error_cls=ValueError),
            _make_mock_task("dd"),
        ]
        collected, timings, errors = orch._collect_parallel(tasks)

        assert len(collected) == 2
        assert len(errors) == 1
        assert "gh" in errors[0]
        assert "ValueError" in errors[0] or "boom" in errors[0]

    # 13. None values filtered from results
    def test_results_filtered(self) -> None:
        """None data from failed collectors is excluded."""
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        tasks = [
            _make_mock_task("cw"),
            _make_mock_task("gh", fail=True),
            _make_mock_task("gl"),
        ]
        collected, _, errors = orch._collect_parallel(tasks)

        # Only non-None results in collected
        assert len(collected) == 2
        sources = [c.source for c in collected]
        assert "cw" in sources
        assert "gl" in sources
        assert len(errors) == 1

    # 14. Zero collectors → empty list, no crash
    def test_zero_collectors_sequential(self) -> None:
        orch = DiagnosisOrchestrator.__new__(DiagnosisOrchestrator)
        collected, timings, errors = orch._collect_sequential([])
        assert collected == []
        assert timings == {}
        assert errors == []

    # 15. CLI --sequential flag wires through
    @patch("autopsy.diagnosis.DiagnosisOrchestrator")
    @patch("autopsy.config.load_config")
    def test_cli_sequential_flag(
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
        result = runner.invoke(
            cli, ["diagnose", "--sequential"], catch_exceptions=False
        )

        assert result.exit_code == 0
        mock_orch.run.assert_called_once()
        call_kwargs = mock_orch.run.call_args[1]
        assert call_kwargs["sequential"] is True


# ---------------------------------------------------------------------------
# Multi-source orchestrator tests
# ---------------------------------------------------------------------------


class TestMultiSourceOrchestrator:
    """Tests for multi-source diagnosis pipeline."""

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    @patch("autopsy.diagnosis.DatadogCollector")
    def test_multi_source_sources_populated(
        self,
        mock_dd_cls: MagicMock,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """result.sources is populated with SourceInfo for each collector."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")
        monkeypatch.setenv("DD_API_KEY", "dd-test")
        monkeypatch.setenv("DD_APP_KEY", "dd-app-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        cw_data = MagicMock(source="cloudwatch", data_type="logs", entries=[1, 2, 3])
        mock_cw.collect.return_value = cw_data
        mock_cw_cls.return_value = mock_cw

        mock_dd = MagicMock()
        mock_dd.validate_config.return_value = True
        dd_data = MagicMock(source="datadog", data_type="logs", entries=[1, 2])
        mock_dd.collect.return_value = dd_data
        mock_dd_cls.return_value = mock_dd

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        gh_data = MagicMock(source="github", data_type="deploys", entries=[1])
        mock_gh.collect.return_value = gh_data
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = AutopsyConfig(
            aws=AWSConfig(region="us-east-1", log_groups=["/test"], time_window=30),
            datadog=DatadogConfig(site="us1", time_window=30),
            github=GitHubConfig(
                repo="o/r",
                token_env="GITHUB_TOKEN",
                deploy_count=5,
                branch="main",
            ),
            ai=AIConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
            output=OutputConfig(),
        )
        orch = DiagnosisOrchestrator(config)
        result = orch.run()

        assert len(result.sources) == 3
        names = {s.name for s in result.sources}
        assert names == {"cloudwatch", "datadog", "github"}
        cw_source = next(s for s in result.sources if s.name == "cloudwatch")
        assert cw_source.entry_count == 3
        assert cw_source.data_type == "logs"

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_source_filter_single(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """source_filter limits to specific collectors."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock(source="cloudwatch", data_type="logs", entries=[1])
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(source="github", data_type="deploys", entries=[1])
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        orch.run(source_filter=("cloudwatch",))

        call_data = mock_engine_cls.return_value.diagnose.call_args[0][0]
        sources = [d.source for d in call_data]
        assert "cloudwatch" in sources
        assert "github" not in sources

    def test_source_filter_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Invalid source_filter raises ConfigError with hint."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        from autopsy.utils.errors import ConfigError

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        with pytest.raises(ConfigError, match="not configured"):
            orch.run(source_filter=("elasticsearch",))

    @patch("autopsy.diagnosis.AIEngine")
    @patch("autopsy.diagnosis.GitHubCollector")
    @patch("autopsy.diagnosis.CloudWatchCollector")
    def test_source_filter_multiple(
        self,
        mock_cw_cls: MagicMock,
        mock_gh_cls: MagicMock,
        mock_engine_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """source_filter with multiple values selects those collectors."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

        mock_cw = MagicMock()
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock(source="cloudwatch", data_type="logs", entries=[1])
        mock_cw_cls.return_value = mock_cw

        mock_gh = MagicMock()
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(source="github", data_type="deploys", entries=[1])
        mock_gh_cls.return_value = mock_gh

        mock_engine_cls.return_value.diagnose.return_value = _sample_result()

        config = _minimal_config()
        orch = DiagnosisOrchestrator(config)
        orch.run(source_filter=("cloudwatch", "github"))

        call_data = mock_engine_cls.return_value.diagnose.call_args[0][0]
        assert len(call_data) == 2

    @patch("autopsy.diagnosis.DiagnosisOrchestrator")
    @patch("autopsy.config.load_config")
    def test_cli_source_flag(
        self,
        mock_load_config: MagicMock,
        mock_orch_cls: MagicMock,
    ) -> None:
        """CLI --source flag is wired through to orchestrator.run()."""
        from click.testing import CliRunner

        from autopsy.cli import cli

        mock_load_config.return_value = _minimal_config()
        mock_orch = MagicMock()
        mock_orch.run.return_value = _sample_result()
        mock_orch_cls.return_value = mock_orch

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["diagnose", "--source", "cloudwatch", "--source", "github"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        call_kwargs = mock_orch.run.call_args[1]
        assert call_kwargs["source_filter"] == ("cloudwatch", "github")
