"""Config loader, validator, and interactive init wizard.

Handles reading, writing, and validating the user configuration
stored at ~/.signalfx/config.yaml. The init wizard guides first-time
setup interactively using Rich prompts.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from signalfx.utils.errors import ConfigNotFoundError, ConfigValidationError

CONFIG_DIR = Path.home() / ".signalfx"
CONFIG_PATH = CONFIG_DIR / "config.yaml"

_AWS_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+-\d+)$")
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

console = Console()


# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class AWSConfig(BaseModel):
    """AWS-specific configuration."""

    region: str = "us-east-1"
    log_groups: list[str] = Field(min_length=1)
    time_window: int = Field(default=30, ge=5, le=60)
    profile: str | None = None

    @field_validator("region")
    @classmethod
    def _check_region(cls, v: str) -> str:
        if not _AWS_REGION_RE.match(v):
            msg = f"Invalid AWS region format: '{v}' (expected e.g. us-east-1)"
            raise ValueError(msg)
        return v

    @field_validator("log_groups")
    @classmethod
    def _check_log_groups(cls, v: list[str]) -> list[str]:
        for lg in v:
            if not lg.startswith("/"):
                msg = f"Log group must start with '/': '{lg}'"
                raise ValueError(msg)
        return v


class GitHubConfig(BaseModel):
    """GitHub-specific configuration."""

    repo: str = Field(description="owner/repo format")
    token_env: str = "GITHUB_TOKEN"
    deploy_count: int = Field(default=5, ge=1, le=20)
    branch: str = "main"

    @field_validator("repo")
    @classmethod
    def _check_repo(cls, v: str) -> str:
        if not _REPO_RE.match(v):
            msg = f"GitHub repo must be 'owner/repo' format: '{v}'"
            raise ValueError(msg)
        return v


class AIConfig(BaseModel):
    """AI provider configuration."""

    provider: str = Field(default="anthropic", pattern=r"^(anthropic|openai)$")
    model: str = "claude-sonnet-4-20250514"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = Field(default=4096, ge=256, le=16384)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)


class OutputConfig(BaseModel):
    """Output format configuration."""

    format: str = Field(default="terminal", pattern=r"^(terminal|json|markdown)$")
    verbosity: str = Field(default="normal", pattern=r"^(quiet|normal|verbose)$")


class SignalFXConfig(BaseModel):
    """Top-level configuration model for ~/.signalfx/config.yaml."""

    version: int = 1
    aws: AWSConfig
    github: GitHubConfig
    ai: AIConfig = Field(default_factory=AIConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _validate_env_var_names(self) -> SignalFXConfig:
        """Ensure env var field names are non-empty strings."""
        if not self.github.token_env:
            msg = "github.token_env must be a non-empty env var name"
            raise ValueError(msg)
        if not self.ai.api_key_env:
            msg = "ai.api_key_env must be a non-empty env var name"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> SignalFXConfig:
    """Read ~/.signalfx/config.yaml and return a validated SignalFXConfig.

    Args:
        path: Path to the YAML config file. Defaults to CONFIG_PATH.

    Returns:
        Validated SignalFXConfig instance.

    Raises:
        ConfigNotFoundError: If the config file does not exist.
        ConfigValidationError: If the config fails Pydantic validation.
    """
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        raise ConfigNotFoundError(
            message=f"Config file not found: {path}",
            hint="Run 'signalfx init' to create a config file.",
        )

    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            message=f"Failed to parse YAML: {exc}",
            hint="Check your config file for syntax errors.",
        ) from exc

    if not isinstance(data, dict):
        raise ConfigValidationError(
            message="Config file must contain a YAML mapping at the top level.",
            hint="Run 'signalfx init' to regenerate a valid config.",
        )

    try:
        return SignalFXConfig(**data)
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigValidationError(
            message=f"Config validation failed: {errors}",
            hint="Fix the values in your config file or re-run 'signalfx init'.",
        ) from exc


def validate_config(config: SignalFXConfig) -> dict[str, bool]:
    """Check that referenced env vars exist (not their values, just existence).

    Args:
        config: Validated config object.

    Returns:
        Dict mapping env var names to True (set & non-empty) or False.
    """
    env_vars = {
        config.github.token_env: bool(os.environ.get(config.github.token_env)),
        config.ai.api_key_env: bool(os.environ.get(config.ai.api_key_env)),
    }
    return env_vars


def save_config(config: SignalFXConfig, path: Path | None = None) -> Path:
    """Serialize a SignalFXConfig to YAML and write to disk.

    Args:
        config: Validated config to write.
        path: Destination path.

    Returns:
        Path that was written.
    """
    if path is None:
        path = CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump()
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return path


def mask_secrets(config: SignalFXConfig) -> dict:
    """Return config dict with secret env var values masked.

    The env var *names* are shown; actual runtime values are replaced
    with '****' unless --reveal is used (handled by the caller).

    Args:
        config: Validated config.

    Returns:
        Dict representation safe for display.
    """
    data = config.model_dump()
    for section_key, field_name in [("github", "token_env"), ("ai", "api_key_env")]:
        env_name = data[section_key][field_name]
        env_val = os.environ.get(env_name, "")
        data[section_key][f"{field_name}_value"] = "****" if env_val else "(not set)"
    return data


def init_wizard(config_path: Path | None = None) -> Path:
    """Interactive setup wizard using Rich prompts.

    Guides the user through first-time configuration and writes
    ~/.signalfx/config.yaml with sensible defaults.

    Args:
        config_path: Where to write the resulting config.

    Returns:
        Path to the created config file.

    Raises:
        ConfigValidationError: If the user provides invalid values.
    """
    if config_path is None:
        config_path = CONFIG_PATH

    console.print(
        Panel(
            "[bold]Welcome to SignalFX[/bold]\n"
            "This wizard will create your configuration file.",
            title="signalfx init",
            border_style="blue",
        )
    )

    # --- AWS ---
    console.print("\n[bold cyan]AWS CloudWatch Configuration[/bold cyan]")
    region = Prompt.ask("AWS region", default="us-east-1")
    log_groups_raw = Prompt.ask(
        "CloudWatch log groups (comma-separated)",
        default="/aws/lambda/my-api",
    )
    log_groups = [lg.strip() for lg in log_groups_raw.split(",") if lg.strip()]
    time_window = int(Prompt.ask("Time window (minutes, 5-60)", default="30"))
    aws_profile = Prompt.ask("AWS CLI profile (leave blank for default)", default="")

    # --- GitHub ---
    console.print("\n[bold cyan]GitHub Configuration[/bold cyan]")
    repo = Prompt.ask("GitHub repo (owner/repo)", default="owner/repo")
    token_env = Prompt.ask("Env var for GitHub token", default="GITHUB_TOKEN")
    deploy_count = int(Prompt.ask("Recent deploys to analyze", default="5"))
    branch = Prompt.ask("Branch to track", default="main")

    # --- AI ---
    console.print("\n[bold cyan]AI Provider Configuration[/bold cyan]")
    provider = Prompt.ask("AI provider", choices=["anthropic", "openai"], default="anthropic")
    if provider == "anthropic":
        default_model = "claude-sonnet-4-20250514"
        default_key_env = "ANTHROPIC_API_KEY"
    else:
        default_model = "gpt-4o"
        default_key_env = "OPENAI_API_KEY"

    model = Prompt.ask("Model name", default=default_model)
    api_key_env = Prompt.ask("Env var for API key", default=default_key_env)

    try:
        config = SignalFXConfig(
            aws=AWSConfig(
                region=region,
                log_groups=log_groups,
                time_window=time_window,
                profile=aws_profile or None,
            ),
            github=GitHubConfig(
                repo=repo,
                token_env=token_env,
                deploy_count=deploy_count,
                branch=branch,
            ),
            ai=AIConfig(
                provider=provider,
                model=model,
                api_key_env=api_key_env,
            ),
        )
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigValidationError(
            message=f"Invalid configuration: {errors}",
            hint="Re-run 'signalfx init' and double-check your inputs.",
        ) from exc

    written = save_config(config, config_path)

    console.print()
    _render_config_summary(config)
    console.print(f"\n[green]✔ Config written to {written}[/green]")

    env_status = validate_config(config)
    missing = [k for k, v in env_status.items() if not v]
    if missing:
        warn = Text()
        warn.append("\n⚠ Missing env vars: ", style="yellow")
        warn.append(", ".join(missing), style="bold yellow")
        warn.append(
            "\n  Set them before running 'signalfx diagnose'.",
            style="yellow",
        )
        console.print(warn)

    return written


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_config_summary(config: SignalFXConfig) -> None:
    """Print a Rich summary of the config (for init wizard confirmation)."""
    lines = [
        f"[bold]AWS[/bold]  region={config.aws.region}  "
        f"log_groups={config.aws.log_groups}  "
        f"window={config.aws.time_window}m",
        f"[bold]GitHub[/bold]  repo={config.github.repo}  "
        f"branch={config.github.branch}  "
        f"deploys={config.github.deploy_count}",
        f"[bold]AI[/bold]  provider={config.ai.provider}  "
        f"model={config.ai.model}",
    ]
    console.print(
        Panel("\n".join(lines), title="Configuration Summary", border_style="green")
    )
