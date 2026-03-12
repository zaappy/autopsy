"""Tests for autopsy.config — config loading, validation, init wizard."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
import yaml
from pydantic import ValidationError

from autopsy.config import (
    AIConfig,
    AutopsyConfig,
    DatadogConfig,
    _write_env_file,
    load_config,
    load_env,
    save_config,
    validate_config,
)
from autopsy.utils.errors import ConfigNotFoundError, ConfigValidationError

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config_dict() -> dict:
    """Smallest valid config as a plain dict."""
    return {
        "version": 1,
        "aws": {
            "region": "us-east-1",
            "log_groups": ["/aws/lambda/my-api"],
        },
        "github": {
            "repo": "owner/repo",
        },
    }


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Pydantic model unit tests
# ---------------------------------------------------------------------------


class TestAutopsyConfig:
    """Validation rules on the Pydantic models."""

    def test_minimal_valid(self) -> None:
        cfg = AutopsyConfig(**_minimal_config_dict())
        assert cfg.aws.region == "us-east-1"
        assert cfg.github.repo == "owner/repo"
        assert cfg.ai.provider == "anthropic"
        assert cfg.output.format == "terminal"

    def test_defaults_filled(self) -> None:
        cfg = AutopsyConfig(**_minimal_config_dict())
        assert cfg.aws.time_window == 30
        assert cfg.github.deploy_count == 5
        assert cfg.ai.temperature == 0.2
        assert cfg.output.verbosity == "normal"
        assert cfg.datadog is None

    def test_invalid_region_rejected(self) -> None:
        data = _minimal_config_dict()
        data["aws"]["region"] = "not-a-region"
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_log_group_must_start_with_slash(self) -> None:
        data = _minimal_config_dict()
        data["aws"]["log_groups"] = ["no-slash"]
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_empty_log_groups_rejected(self) -> None:
        data = _minimal_config_dict()
        data["aws"]["log_groups"] = []
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_repo_format_enforced(self) -> None:
        data = _minimal_config_dict()
        data["github"]["repo"] = "noslash"
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_invalid_provider_rejected(self) -> None:
        data = _minimal_config_dict()
        data["ai"] = {"provider": "google"}
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_temperature_out_of_range(self) -> None:
        data = _minimal_config_dict()
        data["ai"] = {"provider": "anthropic", "temperature": 2.0}
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_time_window_bounds(self) -> None:
        data = _minimal_config_dict()
        data["aws"]["time_window"] = 3
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

        data["aws"]["time_window"] = 120
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)

    def test_multiple_log_groups(self) -> None:
        data = _minimal_config_dict()
        data["aws"]["log_groups"] = ["/aws/lambda/a", "/ecs/b"]
        cfg = AutopsyConfig(**data)
        assert len(cfg.aws.log_groups) == 2

    def test_openai_provider_accepted(self) -> None:
        data = _minimal_config_dict()
        data["ai"] = {"provider": "openai"}
        cfg = AutopsyConfig(**data)
        assert cfg.ai.provider == "openai"

    def test_datadog_optional_and_defaults(self) -> None:
        data = _minimal_config_dict()
        data["datadog"] = {}
        cfg = AutopsyConfig(**data)
        assert cfg.datadog is not None
        assert cfg.datadog.api_key_env == "DD_API_KEY"
        assert cfg.datadog.app_key_env == "DD_APP_KEY"
        assert cfg.datadog.site == "us1"

    def test_invalid_datadog_site_rejected(self) -> None:
        data = _minimal_config_dict()
        data["datadog"] = {"site": "invalid"}
        with pytest.raises(ValidationError):
            AutopsyConfig(**data)


# ---------------------------------------------------------------------------
# load_config tests
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Loading config from YAML files."""

    def test_load_valid_config(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path / "config.yaml", _minimal_config_dict())
        cfg = load_config(path)
        assert cfg.aws.region == "us-east-1"
        assert cfg.github.repo == "owner/repo"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigNotFoundError):
            load_config(tmp_path / "nope.yaml")

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.yaml"
        bad.write_text(": : : not valid yaml [[[", encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            load_config(bad)

    def test_non_dict_yaml_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "config.yaml"
        bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ConfigValidationError):
            load_config(bad)

    def test_schema_violation_raises(self, tmp_path: Path) -> None:
        data = _minimal_config_dict()
        data["aws"]["region"] = "bad!"
        path = _write_yaml(tmp_path / "config.yaml", data)
        with pytest.raises(ConfigValidationError):
            load_config(path)

    def test_roundtrip(self, tmp_path: Path) -> None:
        cfg = AutopsyConfig(**_minimal_config_dict())
        path = save_config(cfg, tmp_path / "out.yaml")
        loaded = load_config(path)
        assert loaded.model_dump() == cfg.model_dump()


# ---------------------------------------------------------------------------
# validate_config tests
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """Credential resolution and source checks."""

    def test_vars_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = AutopsyConfig(**_minimal_config_dict())
        result = validate_config(cfg)
        assert result["github_token"]["set"] is True
        assert result["anthropic_key"]["set"] is True
        assert result["anthropic_key"]["primary"] is True

    def test_vars_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = AutopsyConfig(**_minimal_config_dict())
        result = validate_config(cfg)
        assert result["github_token"]["set"] is False
        assert result["anthropic_key"]["set"] is False
        assert result["anthropic_key"]["primary"] is True

    def test_partial_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = AutopsyConfig(**_minimal_config_dict())
        result = validate_config(cfg)
        assert result["github_token"]["set"] is True
        assert result["anthropic_key"]["set"] is False

    def test_empty_var_counts_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "")
        cfg = AutopsyConfig(**_minimal_config_dict())
        result = validate_config(cfg)
        assert result["github_token"]["set"] is False


# ---------------------------------------------------------------------------
# load_env tests
# ---------------------------------------------------------------------------


class TestLoadEnv:
    """Dotenv loading from ~/.autopsy/.env."""

    def test_load_env_handles_missing_file(self, tmp_path: Path, monkeypatch) -> None:
        """Missing .env is a no-op."""
        monkeypatch.setattr("autopsy.config.ENV_FILE", tmp_path / "nonexistent.env")
        load_env()  # should not raise

    def test_load_env_loads_from_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Variables from .env are loaded into os.environ."""
        env_path = tmp_path / ".env"
        env_path.write_text("TEST_LOAD_ENV_VAR=from_dotenv\n", encoding="utf-8")
        monkeypatch.setattr("autopsy.config.ENV_FILE", env_path)
        monkeypatch.delenv("TEST_LOAD_ENV_VAR", raising=False)
        load_env()
        assert os.environ.get("TEST_LOAD_ENV_VAR") == "from_dotenv"

    def test_load_env_does_not_override_existing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """override=False: pre-existing env vars take precedence."""
        env_path = tmp_path / ".env"
        env_path.write_text("TEST_OVERRIDE_VAR=from_dotenv\n", encoding="utf-8")
        monkeypatch.setattr("autopsy.config.ENV_FILE", env_path)
        monkeypatch.setenv("TEST_OVERRIDE_VAR", "from_environment")
        load_env()
        assert os.environ.get("TEST_OVERRIDE_VAR") == "from_environment"


# ---------------------------------------------------------------------------
# get_active_api_key tests
# ---------------------------------------------------------------------------


class TestGetActiveApiKey:
    """AIConfig.get_active_api_key resolves correct env var."""

    def test_anthropic_primary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = AIConfig(provider="anthropic")
        assert cfg.get_active_api_key() == "sk-ant-xxx"
        assert cfg.get_active_api_key(provider="anthropic") == "sk-ant-xxx"

    def test_openai_primary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
        cfg = AIConfig(provider="openai")
        assert cfg.get_active_api_key() == "sk-xxx"
        assert cfg.get_active_api_key(provider="openai") == "sk-xxx"

    def test_provider_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xxx")
        cfg = AIConfig(provider="anthropic")
        assert cfg.get_active_api_key(provider="openai") == "sk-openai-xxx"
        assert cfg.get_active_api_key(provider="anthropic") == "sk-ant-xxx"

    def test_missing_key_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = AIConfig(provider="anthropic")
        assert cfg.get_active_api_key() == ""


# ---------------------------------------------------------------------------
# _write_env_file tests
# ---------------------------------------------------------------------------


class TestWriteEnvFile:
    """Env file creation and permissions."""

    def test_writes_env_with_correct_format(self, tmp_path: Path) -> None:
        """Env file has expected format and 0o600 permissions."""
        env_path = tmp_path / ".env"
        _write_env_file({"GITHUB_TOKEN": "ghp_xxx", "ANTHROPIC_API_KEY": "sk-ant-xxx"}, env_path)
        content = env_path.read_text(encoding="utf-8")
        assert "GITHUB_TOKEN=ghp_xxx" in content
        assert "ANTHROPIC_API_KEY=sk-ant-xxx" in content
        assert "# Autopsy CLI credentials" in content
        assert env_path.exists()
        # On Unix, verify 0o600 permissions (owner read/write only)
        import sys

        if sys.platform != "win32" and hasattr(env_path.stat(), "st_mode"):
            mode = env_path.stat().st_mode & 0o777
            assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Parent directory is created if missing."""
        env_path = tmp_path / "nested" / "dir" / ".env"
        _write_env_file({"FOO": "bar"}, env_path)
        assert env_path.exists()


# ---------------------------------------------------------------------------
# save_config tests
# ---------------------------------------------------------------------------


class TestSaveConfig:
    """Writing config to disk."""

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        cfg = AutopsyConfig(**_minimal_config_dict())
        path = tmp_path / "deep" / "nested" / "config.yaml"
        result = save_config(cfg, path)
        assert result.exists()

    def test_written_file_is_valid_yaml(self, tmp_path: Path) -> None:
        cfg = AutopsyConfig(**_minimal_config_dict())
        path = save_config(cfg, tmp_path / "config.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["aws"]["region"] == "us-east-1"


# ---------------------------------------------------------------------------
# CLI integration tests (via Click test runner)
# ---------------------------------------------------------------------------


class TestCLI:
    """CLI command smoke tests using Click's CliRunner."""

    def test_version_output(self) -> None:
        from click.testing import CliRunner

        from autopsy.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["version"])
        assert result.exit_code == 0
        assert "autopsy" in result.output
        assert "prompt" in result.output
        assert "python" in result.output

    def test_config_show_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from autopsy import config as config_mod
        from autopsy.cli import cli

        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "nope.yaml")
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code != 0

    def test_config_show_valid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from autopsy import config as config_mod
        from autopsy.cli import cli

        path = _write_yaml(tmp_path / "config.yaml", _minimal_config_dict())
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "us-east-1" in result.output
        assert "Credentials" in result.output
        assert "AI Provider" in result.output

    def test_config_validate_missing_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from autopsy import config as config_mod
        from autopsy.cli import cli

        path = _write_yaml(tmp_path / "config.yaml", _minimal_config_dict())
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "validate"])
        assert result.exit_code != 0
        assert "Missing" in result.output

    def test_config_validate_all_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from click.testing import CliRunner

        from autopsy import config as config_mod
        from autopsy.cli import cli

        path = _write_yaml(tmp_path / "config.yaml", _minimal_config_dict())
        monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setattr(
            config_mod,
            "_check_aws_credentials",
            lambda _: {"found": True, "source": "profile 'default'"},
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "validate"])
        assert result.exit_code == 0
        assert "All credentials are configured" in result.output
