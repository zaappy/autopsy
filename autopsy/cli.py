"""Click CLI entrypoint and command definitions.

This module contains ONLY Click command definitions and argument parsing.
All business logic is delegated to config.py, diagnosis.py, and renderers.
"""

from __future__ import annotations

import sys

import click
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from autopsy.utils.errors import AutopsyError

console = Console(stderr=True)


def _handle_error(err: AutopsyError) -> None:
    """Render an AutopsyError in the red/yellow/green pattern and exit."""
    text = Text()
    text.append(f"❌ {err.message}\n", style="bold red")
    if err.hint:
        text.append(f"\n✅ {err.hint}\n", style="green")
    if err.docs_url:
        text.append(f"\nDocs: {err.docs_url}\n", style="dim")
    console.print(Panel(text, border_style="red"))
    sys.exit(1)


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value:
        return
    from autopsy import PROMPT_VERSION, __version__

    out = Console()
    out.print(f"[bold]autopsy[/bold]  {__version__}")
    out.print(f"[bold]prompt[/bold]    {PROMPT_VERSION}")
    out.print(f"[bold]python[/bold]    {sys.version.split()[0]}")
    ctx.exit()


# Exit codes returned by TUI when user selects Init / Validate / Config (so we run the command)
_TUI_EXIT_INIT = 100
_TUI_EXIT_VALIDATE = 101
_TUI_EXIT_CONFIG_SHOW = 102


@click.group(invoke_without_command=True)
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version,
    expose_value=False,
    is_eager=True,
    help="Show CLI version, prompt version, and Python version.",
)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Autopsy — AI-powered incident diagnosis for engineering teams."""
    # Always load .env first so credentials are available for all commands
    from autopsy.config import load_env

    load_env()

    if ctx.invoked_subcommand is not None:
        return
    try:
        from autopsy.tui import run_tui

        code = run_tui()
    except ImportError:
        click.echo(ctx.get_help())
        return
    if code == _TUI_EXIT_INIT:
        ctx.invoke(init)
    elif code == _TUI_EXIT_VALIDATE:
        ctx.invoke(config_validate)
    elif code == _TUI_EXIT_CONFIG_SHOW:
        ctx.invoke(config_show)


# ---------------------------------------------------------------------------
# autopsy init
# ---------------------------------------------------------------------------


@cli.command()
def init() -> None:
    """Interactive configuration wizard. Creates ~/.autopsy/config.yaml."""
    from autopsy.config import init_wizard

    try:
        init_wizard()
    except AutopsyError as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# autopsy diagnose
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--time-window", type=int, help="Minutes of logs to pull (5-60).")
@click.option("--log-group", "log_groups", multiple=True, help="Override log groups from config.")
@click.option(
    "--provider",
    type=click.Choice(["anthropic", "openai"]),
    help="Override AI provider.",
)
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON instead of panels.")
@click.option("--verbose", is_flag=True, help="Show debug-level detail.")
def diagnose(
    time_window: int | None,
    log_groups: tuple[str, ...],
    provider: str | None,
    output_json: bool,
    verbose: bool,
) -> None:
    """Run AI-powered incident diagnosis."""
    from autopsy.config import load_config
    from autopsy.diagnosis import DiagnosisOrchestrator
    from autopsy.renderers.json_out import JSONRenderer
    from autopsy.renderers.terminal import TerminalRenderer

    try:
        cfg = load_config()
    except AutopsyError as exc:
        _handle_error(exc)

    overrides = {}
    if time_window is not None:
        overrides["time_window"] = time_window
    if log_groups:
        overrides["log_groups"] = list(log_groups)
    if provider is not None:
        overrides["provider"] = provider

    try:
        orchestrator = DiagnosisOrchestrator(cfg)
        result = orchestrator.run(
            time_window=overrides.get("time_window"),
            log_groups=overrides.get("log_groups"),
            provider=overrides.get("provider"),
        )
    except AutopsyError as exc:
        _handle_error(exc)

    if output_json:
        JSONRenderer().render(result)
    else:
        TerminalRenderer().render(result)


# ---------------------------------------------------------------------------
# autopsy config show / validate
# ---------------------------------------------------------------------------


@cli.group()
def config() -> None:
    """Show or edit configuration."""


def _format_credential_status(cfg) -> str:
    """Format credential sources for config show."""
    from autopsy.config import ENV_FILE, validate_config

    status = validate_config(cfg)
    lines = []
    primary = cfg.ai.provider

    # AI provider
    lines.append(f"AI Provider: {primary} (primary)")

    # Anthropic key
    anth = status["anthropic_key"]
    if anth["set"]:
        src = "~/.autopsy/.env" if ENV_FILE.exists() else "environment variable"
        role = " (primary)" if anth["primary"] else " (fallback)"
        lines.append(f"Anthropic Key: {src} ✔{role}")
    else:
        if anth["primary"]:
            lines.append(
                "Anthropic Key: ✘ NOT SET — run 'autopsy init' or export ANTHROPIC_API_KEY"
            )
        else:
            lines.append("Anthropic Key: ✘ NOT SET (optional — needed for --provider anthropic)")

    # OpenAI key
    openai = status["openai_key"]
    if openai["set"]:
        src = "~/.autopsy/.env" if ENV_FILE.exists() else "environment variable"
        role = " (primary)" if openai["primary"] else " (fallback)"
        lines.append(f"OpenAI Key:    {src} ✔{role}")
    else:
        if openai["primary"]:
            lines.append("OpenAI Key:    ✘ NOT SET — run 'autopsy init' or export OPENAI_API_KEY")
        else:
            lines.append("OpenAI Key:    ✘ NOT SET (optional — needed for --provider openai)")

    return "\n".join(lines)


@config.command("show")
@click.option("--reveal", is_flag=True, help="Show secret values unmasked.")
def config_show(reveal: bool) -> None:
    """Print current config (secrets masked by default)."""
    from autopsy.config import load_config, mask_secrets

    try:
        cfg = load_config()
    except AutopsyError as exc:
        _handle_error(exc)

    data = cfg.model_dump() if reveal else mask_secrets(cfg)

    output = Console()
    output.print(
        Panel(
            yaml.dump(data, default_flow_style=False, sort_keys=False).rstrip(),
            title="~/.autopsy/config.yaml",
            border_style="blue",
        )
    )
    output.print(
        Panel(
            _format_credential_status(cfg),
            title="Credentials",
            border_style="cyan",
        )
    )


@config.command("validate")
def config_validate() -> None:
    """Verify credentials and connectivity for all integrations."""
    from autopsy.config import ENV_FILE, load_config, validate_config

    try:
        cfg = load_config()
    except AutopsyError as exc:
        _handle_error(exc)

    status = validate_config(cfg)

    table = Table(title="Credential Check", show_header=True)
    table.add_column("Credential", style="bold")
    table.add_column("Status")
    table.add_column("Source")

    all_ok = True

    # GitHub
    gh = status["github_token"]
    if gh["set"]:
        table.add_row("GitHub Token", "[green]✔[/green]", gh["source"])
    else:
        table.add_row("GitHub Token", "[red]✘ Missing[/red]", gh["source"])
        all_ok = False

    # Anthropic
    anth = status["anthropic_key"]
    if anth["set"]:
        label = " (primary)" if anth["primary"] else ""
        table.add_row(f"Anthropic API Key{label}", "[green]✔[/green]", anth["source"])
    else:
        if anth["primary"]:
            table.add_row("Anthropic API Key (primary)", "[red]✘ Missing[/red]", anth["source"])
            all_ok = False
        else:
            table.add_row(
                "Anthropic API Key (optional)", "[yellow]⚠ not set[/yellow]", anth["source"]
            )

    # OpenAI
    openai = status["openai_key"]
    if openai["set"]:
        label = " (primary)" if openai["primary"] else ""
        table.add_row(f"OpenAI API Key{label}", "[green]✔[/green]", openai["source"])
    else:
        if openai["primary"]:
            table.add_row("OpenAI API Key (primary)", "[red]✘ Missing[/red]", openai["source"])
            all_ok = False
        else:
            table.add_row(
                "OpenAI API Key (optional)", "[yellow]⚠ not set[/yellow]", openai["source"]
            )

    # AWS
    aws = status["aws"]
    if aws["found"]:
        table.add_row("AWS Credentials", "[green]✔[/green]", aws["source"])
    else:
        table.add_row("AWS Credentials", "[red]✘ Not found[/red]", aws["source"])
        all_ok = False

    output = Console()
    if ENV_FILE.exists():
        output.print(f"\n[dim]Credentials loaded from: {ENV_FILE}[/dim]")
    output.print(table)

    if all_ok:
        output.print("\n[green]All credentials are configured.[/green]")
    else:
        output.print(
            "\n[yellow]⚠ Run 'autopsy init' or set the missing credentials "
            "before running 'autopsy diagnose'.[/yellow]"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# autopsy version
# ---------------------------------------------------------------------------


@cli.command()
def version() -> None:
    """Print CLI version, prompt version, and Python version."""
    from autopsy import PROMPT_VERSION, __version__

    output = Console()
    output.print(f"[bold]autopsy[/bold]  {__version__}")
    output.print(f"[bold]prompt[/bold]    {PROMPT_VERSION}")
    output.print(f"[bold]python[/bold]    {sys.version.split()[0]}")
