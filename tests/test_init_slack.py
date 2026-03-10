"""Tests for Slack-only init flow."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from click.testing import CliRunner

from autopsy.cli import cli
from autopsy.config import (
    CONFIG_PATH,
    ENV_FILE,
    AIConfig,
    AutopsyConfig,
    AWSConfig,
    GitHubConfig,
    OutputConfig,
    SlackConfig,
    save_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from autopsy.ai.models import DiagnosisResult


class _DummySlackRenderer:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self.render_calls: list[DiagnosisResult] = []

    def render(self, result: DiagnosisResult) -> bool:
        self.render_calls.append(result)
        return True


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CONFIG_PATH and ENV_FILE to a temporary directory for these tests."""
    cfg_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    monkeypatch.setattr("autopsy.config.CONFIG_PATH", cfg_path, raising=False)
    monkeypatch.setattr("autopsy.config.ENV_FILE", env_path, raising=False)
    monkeypatch.setenv("AUTOPSY_SLACK_WEBHOOK", "", prepend=False)


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


def test_init_slack_updates_env_and_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Prepare an existing config
    cfg = _minimal_config_with_slack()
    save_config(cfg, CONFIG_PATH)

    dummy_renderer = _DummySlackRenderer("https://hooks.slack.test/url")

    def _dummy_slack_renderer(webhook_url: str) -> Any:
        assert webhook_url == "https://hooks.slack.test/url"
        return dummy_renderer

    monkeypatch.setattr("autopsy.renderers.slack.SlackRenderer", _dummy_slack_renderer)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["init", "--slack"],
        input="https://hooks.slack.test/url\n#incidents\n",
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    # SlackRenderer.render should have been called exactly once
    assert len(dummy_renderer.render_calls) == 1

    # .env should contain the webhook URL
    env_text = ENV_FILE.read_text(encoding="utf-8")
    assert "AUTOPSY_SLACK_WEBHOOK=https://hooks.slack.test/url" in env_text

    # Config should include a slack section with enabled=True
    new_cfg_raw = CONFIG_PATH.read_text(encoding="utf-8")
    assert "slack:" in new_cfg_raw
    assert "enabled: true" in new_cfg_raw


def test_config_validate_includes_slack(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from autopsy.cli import config_validate
    from autopsy.config import validate_config

    cfg = _minimal_config_with_slack()
    save_config(cfg, CONFIG_PATH)
    monkeypatch.setenv("AUTOPSY_SLACK_WEBHOOK", "https://hooks.slack.test/url", prepend=False)

    status = validate_config(cfg)
    assert "slack" in status
    assert status["slack"]["configured"] is True
    assert status["slack"]["channel"] == "#incidents"

    runner = CliRunner()
    result = runner.invoke(config_validate, [], catch_exceptions=False)
    assert result.exit_code == 0
    out = result.output
    assert "Slack" in out
    assert "webhook" in out or "channel #incidents" in out

