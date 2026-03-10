"""Tests for SlackRenderer and Slack CLI integration."""

from __future__ import annotations

import json
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from autopsy.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SuggestedFix,
    TimelineEvent,
)
from autopsy.config import (
    AIConfig,
    AutopsyConfig,
    AWSConfig,
    GitHubConfig,
    OutputConfig,
    SlackConfig,
)
from autopsy.renderers.slack import SlackRenderer
from autopsy.utils.errors import SlackSendError


def _sample_result() -> DiagnosisResult:
    return DiagnosisResult(
        root_cause=RootCause(
            summary="NullPointerException in PaymentService.processRefund()",
            category="code_change",
            confidence=0.92,
            evidence=[
                "847x NullPointerException at PaymentService.java:142",
                "First seen 02:47:12Z — 2 min after deploy",
                "Isolated to /api/v2/refunds endpoint",
            ],
        ),
        correlated_deploy=CorrelatedDeploy(
            commit_sha="e4a91bc3deadbeef0000000000000000",
            author="sarah-chen",
            pr_title="Refactor payment metadata handling",
            changed_files=["PaymentService.java", "RefundProcessor.java"],
        ),
        suggested_fix=SuggestedFix(
            immediate="Revert e4a91bc3 or add null-check at PaymentService.java:140",
            long_term="Add @NonNull annotation + integration test for refund flow",
        ),
        timeline=[
            TimelineEvent(time="02:45:01Z", event="Deploy e4a91bc3 merged"),
            TimelineEvent(time="02:47:12Z", event="First error"),
            TimelineEvent(time="02:47:14Z", event="Error rate: 0.1% → 12.4%"),
            TimelineEvent(time="02:48:30Z", event="PagerDuty alert triggered"),
        ],
    )


def test_build_blocks_with_full_result_structure() -> None:
    r = SlackRenderer("https://hooks.slack.test")
    result = _sample_result()

    blocks = r._build_blocks(result)

    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "header"
    root_section = blocks[2]
    assert root_section["type"] == "section"
    text = root_section["text"]["text"]
    assert "Root Cause" in text
    assert "92% confidence" in text
    assert "`code_change`" in text


def test_build_blocks_without_deploy_omits_deploy_section() -> None:
    r = SlackRenderer("https://hooks.slack.test")
    result = _sample_result()
    result.correlated_deploy.commit_sha = None
    result.correlated_deploy.author = None
    result.correlated_deploy.pr_title = None
    result.correlated_deploy.changed_files = []

    blocks = r._build_blocks(result)

    text_blob = json.dumps(blocks)
    assert "Correlated Deploy" not in text_blob


def test_build_blocks_truncates_evidence_and_timeline() -> None:
    r = SlackRenderer("https://hooks.slack.test")
    result = _sample_result()
    # Make many evidence lines and timeline events
    result.root_cause.evidence = [f"evidence {i}" for i in range(10)]
    result.timeline = [TimelineEvent(time=f"t{i}", event=f"event {i}") for i in range(10)]

    blocks = r._build_blocks(result)
    evidence_block = blocks[3]["text"]["text"]
    assert evidence_block.count("•") == 5

    text_blob = json.dumps(blocks)
    assert "event 0" in text_blob
    # Only first 6 events should be present
    assert "event 6" not in text_blob


def test_build_blocks_confidence_emoji_thresholds() -> None:
    r = SlackRenderer("https://hooks.slack.test")
    result = _sample_result()

    # Green >= 0.75
    result.root_cause.confidence = 0.9
    green = r._build_blocks(result)[2]["text"]["text"]
    assert "🟢" in green

    # Yellow >= 0.5
    result.root_cause.confidence = 0.5
    yellow = r._build_blocks(result)[2]["text"]["text"]
    assert "🟡" in yellow

    # Red < 0.5
    result.root_cause.confidence = 0.3
    red = r._build_blocks(result)[2]["text"]["text"]
    assert "🔴" in red


@patch("urllib.request.urlopen")
def test_render_posts_to_webhook_and_returns_true(mock_urlopen: MagicMock) -> None:
    resp = SimpleNamespace(status=200)
    mock_urlopen.return_value.__enter__.return_value = resp

    r = SlackRenderer("https://hooks.slack.test")
    ok = r.render(_sample_result())

    assert ok is True
    mock_urlopen.assert_called_once()
    args, kwargs = mock_urlopen.call_args
    assert "timeout" in kwargs and kwargs["timeout"] == 10
    req = args[0]
    assert isinstance(req, urllib.request.Request) or hasattr(req, "data")


@patch("urllib.request.urlopen")
def test_render_raises_on_non_200_response(mock_urlopen: MagicMock) -> None:
    resp = SimpleNamespace(status=500)
    mock_urlopen.return_value.__enter__.return_value = resp

    r = SlackRenderer("https://hooks.slack.test")
    try:
        r.render(_sample_result())
    except SlackSendError as exc:
        assert "non-200" in exc.message
    else:
        raise AssertionError("SlackSendError was not raised")


@patch("urllib.request.urlopen")
def test_render_raises_slack_send_error_on_network_failure(mock_urlopen: MagicMock) -> None:
    mock_urlopen.side_effect = OSError("network down")

    r = SlackRenderer("https://hooks.slack.test")
    try:
        r.render(_sample_result())
    except SlackSendError as exc:
        assert "Failed to send to Slack" in exc.message
    else:
        raise AssertionError("SlackSendError was not raised")


def _minimal_config_with_slack() -> AutopsyConfig:
    return AutopsyConfig(
        aws=AWSConfig(region="us-east-1", log_groups=["/aws/lambda/test"], time_window=30),
        github=GitHubConfig(
            repo="owner/repo",
            token_env="GITHUB_TOKEN",
            deploy_count=5,
            branch="main",
        ),
        ai=AIConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
        output=OutputConfig(),
        slack=SlackConfig(
            webhook_url_env="AUTOPSY_SLACK_WEBHOOK",
            channel="#incidents",
            enabled=True,
        ),
    )


def test_resolve_slack_webhook_reads_env(monkeypatch) -> None:
    from autopsy.cli import _resolve_slack_webhook

    cfg = _minimal_config_with_slack()
    monkeypatch.setenv("AUTOPSY_SLACK_WEBHOOK", "https://hooks.slack.test/url")

    url = _resolve_slack_webhook(cfg)
    assert url == "https://hooks.slack.test/url"


def test_resolve_slack_webhook_returns_empty_when_disabled(monkeypatch) -> None:
    from autopsy.cli import _resolve_slack_webhook

    cfg = _minimal_config_with_slack()
    cfg.slack.enabled = False  # type: ignore[assignment]
    monkeypatch.delenv("AUTOPSY_SLACK_WEBHOOK", raising=False)

    url = _resolve_slack_webhook(cfg)
    assert url == ""


@patch("autopsy.config.load_config")
@patch("autopsy.diagnosis.DiagnosisOrchestrator")
@patch("autopsy.renderers.slack.SlackRenderer")
def test_cli_diagnose_slack_flag_posts_to_slack(
    mock_slack_renderer_cls: MagicMock,
    mock_orch_cls: MagicMock,
    mock_load_config: MagicMock,
) -> None:
    from click.testing import CliRunner

    from autopsy.cli import cli

    cfg = _minimal_config_with_slack()
    mock_load_config.return_value = cfg
    mock_result = _sample_result()
    mock_orch = mock_orch_cls.return_value
    mock_orch.run.return_value = mock_result

    mock_slack_renderer = mock_slack_renderer_cls.return_value
    mock_slack_renderer.render.return_value = True

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["diagnose", "--slack"],
        catch_exceptions=False,
        env={"AUTOPSY_SLACK_WEBHOOK": "https://hooks.slack.test/url"},
    )

    assert result.exit_code == 0
    mock_slack_renderer_cls.assert_called_once_with("https://hooks.slack.test/url")
    mock_slack_renderer.render.assert_called_once_with(mock_result)

