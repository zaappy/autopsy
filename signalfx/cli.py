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

from signalfx.utils.errors import SignalFXError

console = Console(stderr=True)


def _handle_error(err: SignalFXError) -> None:
    """Render a SignalFXError in the red/yellow/green pattern and exit."""
    text = Text()
    text.append(f"❌ {err.message}\n", style="bold red")
    if err.hint:
        text.append(f"\n✅ {err.hint}\n", style="green")
    if err.docs_url:
        text.append(f"\nDocs: {err.docs_url}\n", style="dim")
    console.print(Panel(text, border_style="red"))
    sys.exit(1)


@click.group()
def cli() -> None:
    """SignalFX — AI-powered incident diagnosis for engineering teams."""


# ---------------------------------------------------------------------------
# signalfx init
# ---------------------------------------------------------------------------


@cli.command()
def init() -> None:
    """Interactive configuration wizard. Creates ~/.signalfx/config.yaml."""
    from signalfx.config import init_wizard

    try:
        init_wizard()
    except SignalFXError as exc:
        _handle_error(exc)


# ---------------------------------------------------------------------------
# signalfx diagnose
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--time-window", type=int, help="Minutes of logs to pull (5-60).")
@click.option("--log-group", multiple=True, help="Override log groups from config.")
@click.option(
    "--provider",
    type=click.Choice(["anthropic", "openai"]),
    help="Override AI provider.",
)
@click.option("--json", "output_json", is_flag=True, help="Output raw JSON instead of panels.")
@click.option("--verbose", is_flag=True, help="Show debug-level detail.")
def diagnose(
    time_window: int | None,
    log_group: tuple[str, ...],
    provider: str | None,
    output_json: bool,
    verbose: bool,
) -> None:
    """Run AI-powered incident diagnosis."""
    console.print("[yellow]Diagnosis engine not yet implemented.[/yellow]")
    sys.exit(1)


# ---------------------------------------------------------------------------
# signalfx config show / validate
# ---------------------------------------------------------------------------


@cli.group()
def config() -> None:
    """Show or edit configuration."""


@config.command("show")
@click.option("--reveal", is_flag=True, help="Show secret values unmasked.")
def config_show(reveal: bool) -> None:
    """Print current config (secrets masked by default)."""
    from signalfx.config import load_config, mask_secrets

    try:
        cfg = load_config()
    except SignalFXError as exc:
        _handle_error(exc)

    data = cfg.model_dump() if reveal else mask_secrets(cfg)

    output = Console()
    output.print(
        Panel(
            yaml.dump(data, default_flow_style=False, sort_keys=False).rstrip(),
            title="~/.signalfx/config.yaml",
            border_style="blue",
        )
    )


@config.command("validate")
def config_validate() -> None:
    """Verify credentials and connectivity for all integrations."""
    from signalfx.config import load_config, validate_config

    try:
        cfg = load_config()
    except SignalFXError as exc:
        _handle_error(exc)

    env_status = validate_config(cfg)

    table = Table(title="Credential Check", show_header=True)
    table.add_column("Env Var", style="bold")
    table.add_column("Status")

    all_ok = True
    for var_name, is_set in env_status.items():
        if is_set:
            table.add_row(var_name, "[green]✔ Set[/green]")
        else:
            table.add_row(var_name, "[red]✘ Missing[/red]")
            all_ok = False

    output = Console()
    output.print(table)

    if all_ok:
        output.print("\n[green]All credentials are configured.[/green]")
    else:
        output.print(
            "\n[yellow]⚠ Set the missing env vars before "
            "running 'signalfx diagnose'.[/yellow]"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# signalfx version
# ---------------------------------------------------------------------------


@cli.command()
def version() -> None:
    """Print CLI version, prompt version, and Python version."""
    from signalfx import PROMPT_VERSION, __version__

    output = Console()
    output.print(f"[bold]signalfx[/bold]  {__version__}")
    output.print(f"[bold]prompt[/bold]    {PROMPT_VERSION}")
    output.print(f"[bold]python[/bold]    {sys.version.split()[0]}")
