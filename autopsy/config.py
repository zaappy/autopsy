"""Config loader, validator, and interactive init wizard.

Handles reading, writing, and validating the user configuration
stored at ~/.autopsy/config.yaml. The init wizard guides first-time
setup interactively using Rich prompts. Credentials are stored in
~/.autopsy/.env and loaded automatically at runtime.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from autopsy.utils.errors import ConfigNotFoundError, ConfigValidationError, SlackSendError

CONFIG_DIR = Path.home() / ".autopsy"
CONFIG_PATH = CONFIG_DIR / "config.yaml"
ENV_FILE = CONFIG_DIR / ".env"


def load_env() -> None:
    """Load credentials from ~/.autopsy/.env if it exists.

    Uses override=False so real env vars take precedence over .env.
    Power users who export vars in their shell are not affected.
    """
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)


_AWS_REGION_RE = re.compile(r"^[a-z]{2,4}(?:-[a-z][a-z0-9]*)+-\d+$")
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
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    openai_api_key_env: str = "OPENAI_API_KEY"
    max_tokens: int = Field(default=4096, ge=256, le=16384)
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)

    def get_active_api_key(self, provider: str | None = None) -> str:
        """Resolve the API key for the active provider.

        Args:
            provider: Override provider ('anthropic' | 'openai'). Uses config default if None.

        Returns:
            API key string, or empty string if not set.
        """
        p = provider if provider is not None else self.provider
        env_name = self.anthropic_api_key_env if p == "anthropic" else self.openai_api_key_env
        return os.environ.get(env_name, "")


class OutputConfig(BaseModel):
    """Output format configuration."""

    format: str = Field(default="terminal", pattern=r"^(terminal|json|markdown)$")
    verbosity: str = Field(default="normal", pattern=r"^(quiet|normal|verbose)$")


class SlackConfig(BaseModel):
    """Slack integration configuration (optional)."""

    webhook_url_env: str = "AUTOPSY_SLACK_WEBHOOK"
    channel: str = "#incidents"
    enabled: bool = True


class AutopsyConfig(BaseModel):
    """Top-level configuration model for ~/.autopsy/config.yaml."""

    version: int = 1
    aws: AWSConfig
    github: GitHubConfig
    ai: AIConfig = Field(default_factory=AIConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    slack: SlackConfig | None = None

    @model_validator(mode="after")
    def _validate_env_var_names(self) -> AutopsyConfig:
        """Ensure env var field names are non-empty strings."""
        if not self.github.token_env:
            msg = "github.token_env must be a non-empty env var name"
            raise ValueError(msg)
        if not self.ai.anthropic_api_key_env or not self.ai.openai_api_key_env:
            msg = "ai.anthropic_api_key_env and ai.openai_api_key_env must be non-empty"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> AutopsyConfig:
    """Read ~/.autopsy/config.yaml and return a validated AutopsyConfig.

    Args:
        path: Path to the YAML config file. Defaults to CONFIG_PATH.

    Returns:
        Validated AutopsyConfig instance.

    Raises:
        ConfigNotFoundError: If the config file does not exist.
        ConfigValidationError: If the config fails Pydantic validation.
    """
    if path is None:
        path = CONFIG_PATH
    if not path.exists():
        raise ConfigNotFoundError(
            message=f"Config file not found: {path}",
            hint="Run 'autopsy init' to create a config file.",
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
            hint="Run 'autopsy init' to regenerate a valid config.",
        )

    try:
        return AutopsyConfig(**data)
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigValidationError(
            message=f"Config validation failed: {errors}",
            hint="Fix the values in your config file or re-run 'autopsy init'.",
        ) from exc


def validate_config(config: AutopsyConfig) -> dict[str, dict]:
    """Check that credentials can be resolved and optionally validate them.

    Args:
        config: Validated config object.

    Returns:
        Dict mapping credential names to status dicts:
        - github_token: { "set": bool, "source": str }
        - anthropic_key: { "set": bool, "source": str, "primary": bool }
        - openai_key: { "set": bool, "source": str, "primary": bool }
        - aws: { "found": bool, "source": str }
    """
    primary = config.ai.provider
    gh_val = os.environ.get(config.github.token_env, "")
    anth_val = os.environ.get(config.ai.anthropic_api_key_env, "")
    openai_val = os.environ.get(config.ai.openai_api_key_env, "")

    def _src(env_name: str, val: str) -> str:
        if not val:
            return "not set"
        return "~/.autopsy/.env" if ENV_FILE.exists() else "environment"

    slack_cfg = config.slack
    slack_status: dict[str, object] = {
        "configured": False,
        "channel": "",
        "source": "not set",
    }
    if slack_cfg and slack_cfg.enabled:
        slack_env = slack_cfg.webhook_url_env or "AUTOPSY_SLACK_WEBHOOK"
        slack_val = os.environ.get(slack_env, "")
        slack_status["configured"] = bool(slack_val)
        slack_status["channel"] = slack_cfg.channel or "unknown"
        slack_status["source"] = _src(slack_env, slack_val) if slack_val else "not set"

    return {
        "github_token": {
            "set": bool(gh_val),
            "source": _src(config.github.token_env, gh_val),
        },
        "anthropic_key": {
            "set": bool(anth_val),
            "source": _src(config.ai.anthropic_api_key_env, anth_val),
            "primary": primary == "anthropic",
        },
        "openai_key": {
            "set": bool(openai_val),
            "source": _src(config.ai.openai_api_key_env, openai_val),
            "primary": primary == "openai",
        },
        "aws": _check_aws_credentials(config.aws),
        "slack": slack_status,
    }


def _check_aws_credentials(aws_config: AWSConfig) -> dict:
    """Check if boto3 can find AWS credentials."""
    try:
        import boto3

        session = boto3.Session(
            region_name=aws_config.region,
            profile_name=aws_config.profile,
        )
        creds = session.get_credentials()
        if creds is None:
            return {"found": False, "source": "not found"}
        profile = session.profile_name or "default"
        return {"found": True, "source": f"profile '{profile}'"}
    except Exception:
        return {"found": False, "source": "not found"}


def save_config(config: AutopsyConfig, path: Path | None = None) -> Path:
    """Serialize an AutopsyConfig to YAML and write to disk.

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


def mask_secrets(config: AutopsyConfig) -> dict:
    """Return config dict with secret env var values masked.

    The env var *names* are shown; actual runtime values are replaced
    with '****' unless --reveal is used (handled by the caller).

    Args:
        config: Validated config.

    Returns:
        Dict representation safe for display.
    """
    data = config.model_dump()
    gh_env = data["github"]["token_env"]
    data["github"]["token_env_value"] = "****" if os.environ.get(gh_env) else "(not set)"
    anth_env = data["ai"]["anthropic_api_key_env"]
    openai_env = data["ai"]["openai_api_key_env"]
    data["ai"]["anthropic_api_key_value"] = "****" if os.environ.get(anth_env) else "(not set)"
    data["ai"]["openai_api_key_value"] = "****" if os.environ.get(openai_env) else "(not set)"
    return data


def _validate_github_token(token: str, repo: str) -> tuple[bool, str]:
    """Validate GitHub token. Returns (ok, reason) where reason describes any failure."""
    if not token:
        return False, "token is empty"
    try:
        from github import Auth, Github, GithubException

        gh = Github(auth=Auth.Token(token))
        try:
            gh.get_repo(repo)
            gh.close()
            return True, ""
        except GithubException as e:
            gh.close()
            if e.status == 401:
                return False, "token is invalid or expired (401)"
            if e.status == 404:
                return False, f"repo '{repo}' not found or token lacks 'repo' scope (404)"
            return False, f"GitHub error {e.status}: {e.data.get('message', '')}"
    except Exception as e:
        return False, str(e)


def _validate_anthropic_key(key: str) -> bool:
    """Validate Anthropic API key with minimal API call."""
    if not key:
        return False
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=10,
            messages=[{"role": "user", "content": "Hi"}],
        )
        return True
    except Exception:
        return False


def _validate_openai_key(key: str) -> bool:
    """Validate OpenAI API key with minimal API call."""
    if not key:
        return False
    try:
        import openai

        client = openai.OpenAI(api_key=key)
        next(iter(client.models.list()))
        return True
    except Exception:
        return False


def _write_env_file(entries: dict[str, str], env_path: Path | None = None) -> None:
    """Write key=value entries to .env file with 0o600 permissions.

    This helper is used by the full init wizard and overwrites any
    existing file. For incremental updates (e.g. Slack-only init),
    use _update_env_file to merge new keys into an existing file.
    """
    path = env_path or ENV_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Autopsy CLI credentials",
        "# Generated by 'autopsy init' — do not commit to version control",
        "# Location: ~/.autopsy/.env",
        "",
    ]
    for k, v in entries.items():
        if v:
            lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _update_env_file(entries: dict[str, str], env_path: Path | None = None) -> None:
    """Merge key=value entries into .env file without dropping existing keys."""
    path = env_path or ENV_FILE
    existing: dict[str, str] = {}
    if path.exists():
        raw = path.read_text(encoding="utf-8").splitlines()
        for line in raw:
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            existing[key.strip()] = val.strip()

    existing.update({k: v for k, v in entries.items() if v})

    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Autopsy CLI credentials",
        "# Generated by 'autopsy init' — do not commit to version control",
        "# Location: ~/.autopsy/.env",
        "",
    ]
    for key, val in sorted(existing.items()):
        lines.append(f"{key}={val}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def init_wizard(config_path: Path | None = None) -> Path:
    """Interactive setup wizard using Rich prompts.

    Phase 1: Configuration (AWS, GitHub, AI provider, output).
    Phase 2: Credentials (GitHub token, AI keys) — stored in ~/.autopsy/.env.

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
            "[bold]Welcome to Autopsy[/bold]\n"
            "This wizard will create your configuration file and store credentials.",
            title="autopsy init",
            border_style="blue",
        )
    )

    # --- Phase 1: Configuration ---
    # AWS
    console.print("\n[bold cyan]AWS CloudWatch Configuration[/bold cyan]")
    region = Prompt.ask("AWS region", default="us-east-1")
    log_groups_raw = Prompt.ask(
        "CloudWatch log groups (comma-separated)",
        default="/aws/lambda/my-api",
    )
    log_groups = [lg.strip() for lg in log_groups_raw.split(",") if lg.strip()]
    time_window = int(Prompt.ask("Time window (minutes, 5-60)", default="30"))
    aws_profile = Prompt.ask("AWS CLI profile (leave blank for default)", default="")

    # GitHub
    console.print("\n[bold cyan]GitHub Configuration[/bold cyan]")
    repo = Prompt.ask("GitHub repo (owner/repo)", default="owner/repo")
    deploy_count = int(Prompt.ask("Recent deploys to analyze", default="5"))
    branch = Prompt.ask("Branch to track", default="main")

    # AI
    console.print("\n[bold cyan]AI Provider Configuration[/bold cyan]")
    provider = Prompt.ask("AI provider", choices=["anthropic", "openai"], default="anthropic")
    default_model = "claude-sonnet-4-20250514" if provider == "anthropic" else "gpt-4o"
    model = Prompt.ask("Model name", default=default_model)

    try:
        aws_cfg = AWSConfig(
            region=region,
            log_groups=log_groups,
            time_window=time_window,
            profile=aws_profile or None,
        )
    except ValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigValidationError(
            message=f"Invalid configuration: {errors}",
            hint="Re-run 'autopsy init' and double-check your inputs.",
        ) from exc

    # --- Phase 2: Credentials ---
    console.print(
        "\n[bold cyan]🔑 Credentials[/bold cyan]\n"
        "  These are stored locally in ~/.autopsy/.env and never leave your machine.\n"
    )

    env_entries: dict[str, str] = {}

    # GitHub token
    console.print("[dim]Token input is hidden for security (paste and press Enter)[/dim]")
    for _attempt in range(3):
        token = Prompt.ask(
            "GitHub Token (ghp_... or github_pat_...)(⚠ctrl+shift+v to paste)",
            default="",
            password=True,
        ).strip()
        if not token:
            console.print(
                "[yellow]⚠ GitHub token is required. Please enter a valid token.[/yellow]"
            )
            continue
        ok, reason = _validate_github_token(token, repo)
        if ok:
            env_entries["GITHUB_TOKEN"] = token
            console.print("[green]✔ Verified[/green]")
            break
        console.print(f"[red]✘ {reason}. Try again.[/red]")
    else:
        raise ConfigValidationError(
            message="GitHub token validation failed after 3 attempts.",
            hint="Check your token at https://github.com/settings/tokens — needs 'repo' scope",
        )

    # AI keys — primary required, secondary optional
    primary_label = "primary"
    secondary_label = "fallback, optional — press Enter to skip"

    if provider == "anthropic":
        # Anthropic primary
        for _attempt in range(3):
            anth_key = Prompt.ask(
                f"Anthropic API Key ({primary_label})(⚠ctrl+shift+v to paste)",
                default="",
                password=True,
            ).strip()
            if not anth_key:
                console.print("[red]✘ Anthropic key is required (primary provider).[/red]")
                continue
            if _validate_anthropic_key(anth_key):
                env_entries["ANTHROPIC_API_KEY"] = anth_key
                console.print("[green]✔ Verified[/green]")
                break
            console.print("[red]✘ Invalid key. Try again.[/red]")
        else:
            raise ConfigValidationError(
                message="Anthropic API key validation failed after 3 attempts.",
                hint="Get a key at https://console.anthropic.com/",
            )

        # OpenAI optional
        openai_key = Prompt.ask(
            f"OpenAI API Key ({secondary_label})(⚠ctrl+shift+v to paste)",
            default="",
            password=True,
        ).strip()
        if openai_key:
            if _validate_openai_key(openai_key):
                env_entries["OPENAI_API_KEY"] = openai_key
                console.print("[green]✔ Verified[/green]")
            else:
                console.print("[yellow]⚠ OpenAI key invalid — skipped.[/yellow]")
        else:
            console.print("[dim]⏭ Skipped[/dim]")
    else:
        # OpenAI primary
        for _attempt in range(3):
            openai_key = Prompt.ask(
                f"OpenAI API Key ({primary_label})",
                default="",
                password=True,
            ).strip()
            if not openai_key:
                console.print("[red]✘ OpenAI key is required (primary provider).[/red]")
                continue
            if _validate_openai_key(openai_key):
                env_entries["OPENAI_API_KEY"] = openai_key
                console.print("[green]✔ Verified[/green]")
                break
            console.print("[red]✘ Invalid key. Try again.[/red]")
        else:
            raise ConfigValidationError(
                message="OpenAI API key validation failed after 3 attempts.",
                hint="Get a key at https://platform.openai.com/api-keys",
            )

        # Anthropic optional
        anth_key = Prompt.ask(
            f"Anthropic API Key ({secondary_label})",
            default="",
            password=True,
        ).strip()
        if anth_key:
            if _validate_anthropic_key(anth_key):
                env_entries["ANTHROPIC_API_KEY"] = anth_key
                console.print("[green]✔ Verified[/green]")
            else:
                console.print("[yellow]⚠ Anthropic key invalid — skipped.[/yellow]")
        else:
            console.print("[dim]⏭ Skipped[/dim]")

    # AWS — just check, don't ask
    aws_status = _check_aws_credentials(aws_cfg)
    if aws_status["found"]:
        console.print(f"[green]AWS Credentials: ✔ Found via {aws_status['source']}[/green]")
    else:
        console.print(
            "[yellow]AWS Credentials: ✘ Not found. Run 'aws configure' or set AWS_PROFILE.[/yellow]"
        )

    # Write .env
    _write_env_file(env_entries)

    # Build and save config
    config = AutopsyConfig(
        aws=aws_cfg,
        github=GitHubConfig(
            repo=repo,
            token_env="GITHUB_TOKEN",
            deploy_count=deploy_count,
            branch=branch,
        ),
        ai=AIConfig(
            provider=provider,
            model=model,
            anthropic_api_key_env="ANTHROPIC_API_KEY",
            openai_api_key_env="OPENAI_API_KEY",
        ),
    )
    written = save_config(config, config_path)

    console.print()
    _render_config_summary(config)
    console.print(f"\n[green]✔ Config written to {written}[/green]")
    console.print(f"[green]✔ Credentials saved to {ENV_FILE}[/green]")

    return written


def init_slack_only(
    config_path: Path | None = None,
    env_path: Path | None = None,
) -> Path:
    """Slack-only onboarding flow.

    Prompts for an Incoming Webhook URL, sends a test message, writes the
    webhook URL to ~/.autopsy/.env, and updates the slack section of the
    existing config file.
    """
    from autopsy.ai.models import CorrelatedDeploy, DiagnosisResult, RootCause, SuggestedFix
    from autopsy.renderers.slack import SlackRenderer

    if config_path is None:
        config_path = CONFIG_PATH

    try:
        config = load_config(config_path)
    except ConfigNotFoundError as exc:
        raise ConfigValidationError(
            message="Config file not found for Slack setup.",
            hint="Run 'autopsy init' once before 'autopsy init --slack'.",
        ) from exc

    console.print(
        Panel(
            "[bold]Slack integration[/bold]\n"
            "Configure an Incoming Webhook for posting diagnoses to Slack.",
            title="autopsy init --slack",
            border_style="blue",
        )
    )

    webhook_url = Prompt.ask(
        "Slack Incoming Webhook URL (paste, input hidden)",
        password=True,
    ).strip()
    if not webhook_url:
        raise ConfigValidationError(
            message="Slack webhook URL cannot be empty.",
            hint="Generate one at https://api.slack.com/messaging/webhooks.",
        )

    channel = "#incidents"
    if config.slack and config.slack.channel:
        channel = config.slack.channel
    channel = Prompt.ask("Slack channel (display only)", default=channel).strip()

    slack_env = config.slack.webhook_url_env if config.slack else "AUTOPSY_SLACK_WEBHOOK"

    # Send a simple test message via SlackRenderer to validate the webhook URL.
    renderer = SlackRenderer(webhook_url)
    test_result = DiagnosisResult(
        root_cause=RootCause(
            summary="AUTOPSY connected to this Slack channel.",
            category="config_change",
            confidence=1.0,
            evidence=[
                "This is a one-time test message from 'autopsy init --slack'.",
            ],
        ),
        correlated_deploy=CorrelatedDeploy(),
        suggested_fix=SuggestedFix(
            immediate="No action required.",
            long_term="Keep this webhook secret and rotate if compromised.",
        ),
        timeline=[],
    )
    try:
        renderer.render(test_result)
    except SlackSendError as exc:
        raise ConfigValidationError(
            message=f"Failed to send Slack test message: {exc.message}",
            hint=(
                "Verify the webhook URL and network connectivity. "
                "You can regenerate a webhook at: "
                "https://api.slack.com/messaging/webhooks"
            ),
        ) from exc

    _update_env_file({slack_env: webhook_url}, env_path)

    new_slack = SlackConfig(
        webhook_url_env=slack_env,
        channel=channel or "#incidents",
        enabled=True,
    )
    updated = AutopsyConfig(
        version=config.version,
        aws=config.aws,
        github=config.github,
        ai=config.ai,
        output=config.output,
        slack=new_slack,
    )
    written = save_config(updated, config_path)

    console.print(
        f"[green]✔ Slack webhook saved to {written} (channel {new_slack.channel})[/green]"
    )
    if env_path is None:
        env_path = ENV_FILE
    console.print(f"[green]✔ Webhook URL stored in {env_path}[/green]")

    return written


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_config_summary(config: AutopsyConfig) -> None:
    """Print a Rich summary of the config (for init wizard confirmation)."""
    lines = [
        f"[bold]AWS[/bold]  region={config.aws.region}  "
        f"log_groups={config.aws.log_groups}  "
        f"window={config.aws.time_window}m",
        f"[bold]GitHub[/bold]  repo={config.github.repo}  "
        f"branch={config.github.branch}  "
        f"deploys={config.github.deploy_count}",
        f"[bold]AI[/bold]  provider={config.ai.provider}  model={config.ai.model}",
    ]
    console.print(Panel("\n".join(lines), title="Configuration Summary", border_style="green"))
