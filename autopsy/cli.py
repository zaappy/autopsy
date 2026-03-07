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


@click.group()
@click.option(
    "--version",
    is_flag=True,
    callback=_print_version,
    expose_value=False,
    is_eager=True,
    help="Show CLI version, prompt version, and Python version.",
)
def cli() -> None:
    """Autopsy — AI-powered incident diagnosis for engineering teams."""


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


@config.command("validate")
def config_validate() -> None:
    """Verify credentials and connectivity for all integrations."""
    from autopsy.config import load_config, validate_config

    try:
        cfg = load_config()
    except AutopsyError as exc:
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
            "running 'autopsy diagnose'.[/yellow]"
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
