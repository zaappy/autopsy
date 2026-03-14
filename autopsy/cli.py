"""Click CLI entrypoint and command definitions.

This module contains ONLY Click command definitions and argument parsing.
All business logic is delegated to config.py, diagnosis.py, and renderers.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

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


def _resolve_slack_webhook(cfg) -> str:
    """Resolve Slack webhook URL from config/env. Returns empty string if unavailable."""
    slack_cfg = getattr(cfg, "slack", None)
    if not slack_cfg or not slack_cfg.enabled:
        return ""
    env_name = slack_cfg.webhook_url_env or "AUTOPSY_SLACK_WEBHOOK"
    return os.environ.get(env_name, "")


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value:
        return
    from autopsy import PROMPT_VERSION, __version__

    out = Console()
    out.print(f"[bold]autopsy[/bold]  {__version__}")
    out.print(f"[bold]prompt[/bold]    {PROMPT_VERSION}")
    out.print(f"[bold]python[/bold]    {sys.version.split()[0]}")
    ctx.exit()


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
    from autopsy.interactive import run_interactive

    run_interactive()


# ---------------------------------------------------------------------------
# autopsy init
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--slack",
    is_flag=True,
    help="Configure only Slack integration (requires existing config).",
)
def init(slack: bool) -> None:
    """Interactive configuration wizard. Creates ~/.autopsy/config.yaml."""
    from autopsy.config import init_slack_only, init_wizard

    try:
        if slack:
            init_slack_only()
        else:
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
@click.option(
    "--postmortem",
    "-pm",
    is_flag=True,
    help="Generate markdown post-mortem report.",
)
@click.option(
    "--postmortem-path",
    type=click.Path(),
    help="Output path for post-mortem (default: ./postmortem-{date}.md)",
)
@click.option(
    "--slack",
    is_flag=True,
    help="Post diagnosis to Slack.",
)
@click.option(
    "--sequential",
    is_flag=True,
    help="Run collectors sequentially instead of in parallel (debugging).",
)
@click.option(
    "--source",
    "sources",
    multiple=True,
    help="Filter to specific sources (repeatable, e.g., --source cloudwatch --source github).",
)
def diagnose(
    time_window: int | None,
    log_groups: tuple[str, ...],
    provider: str | None,
    output_json: bool,
    verbose: bool,
    postmortem: bool,
    postmortem_path: str | None,
    slack: bool,
    sequential: bool,
    sources: tuple[str, ...],
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

    console.print("[green]✔[/green] Config loaded")

    overrides: dict[str, object] = {}
    if time_window is not None:
        overrides["time_window"] = time_window
    if log_groups:
        overrides["log_groups"] = list(log_groups)
    if provider is not None:
        overrides["provider"] = provider

    # Compute effective settings for metadata (mirrors DiagnosisOrchestrator logic).
    ai_provider = overrides.get("provider") or cfg.ai.provider
    ai_provider_str = str(ai_provider)
    model = (
        cfg.ai.model
        if ai_provider_str == cfg.ai.provider
        else ("gpt-4o" if ai_provider_str == "openai" else cfg.ai.model)
    )
    effective_log_groups = list(overrides.get("log_groups") or cfg.aws.log_groups)
    effective_time_window = int(overrides.get("time_window") or cfg.aws.time_window)

    try:
        orchestrator = DiagnosisOrchestrator(cfg)
        import time as _time

        start = _time.monotonic()
        with console.status("[bold]Running diagnosis pipeline...[/bold]"):
            result = orchestrator.run(
                time_window=overrides.get("time_window"),
                log_groups=overrides.get("log_groups"),
                provider=ai_provider_str,
                sequential=sequential,
                source_filter=sources if sources else None,
            )
        duration_s = round(_time.monotonic() - start, 2)
        console.print(f"[green]✔[/green] Diagnosis complete ({duration_s}s)\n")
    except AutopsyError as exc:
        _handle_error(exc)

    if output_json:
        JSONRenderer().render(result)
    else:
        TerminalRenderer().render(result)

    if postmortem:
        from autopsy.renderers.postmortem import PostMortemMetadata, PostMortemRenderer

        pm = PostMortemRenderer()
        meta = PostMortemMetadata(
            provider=ai_provider_str,
            model=model,
            log_groups=effective_log_groups,
            time_window=effective_time_window,
            duration_s=duration_s,
        )
        if postmortem_path:
            path = Path(postmortem_path)
            output = pm.render(result, output_path=path, metadata=meta)
            console.print(f"[green]✔ Post-mortem saved to {output}[/green]")
        else:
            markdown = pm.render(result, output_path=None, metadata=meta)
            default_path = Path(pm._generate_filename(result))
            default_path.write_text(markdown, encoding="utf-8")
            console.print(f"[green]✔ Post-mortem saved to {default_path}[/green]")

    if slack:
        webhook_url = _resolve_slack_webhook(cfg)
        if webhook_url:
            from autopsy.renderers.slack import SlackRenderer

            slack_renderer = SlackRenderer(webhook_url)
            try:
                slack_renderer.render(result)
                console.print("[green]✔ Diagnosis posted to Slack[/green]")
            except AutopsyError as exc:
                console.print(f"[yellow]⚠ Slack failed: {exc.message}[/yellow]")
        else:
            console.print(
                "[yellow]⚠ Slack not configured. "
                "Set AUTOPSY_SLACK_WEBHOOK in your environment or config.[/yellow]"
            )


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

    # Slack (optional)
    slack = status.get("slack", {"configured": False, "channel": "", "source": "not set"})
    if slack["configured"]:
        chan = slack.get("channel") or "unknown"
        table.add_row("Slack", "[green]✔[/green]", f"channel {chan}")
    else:
        table.add_row("Slack", "[yellow]⚠ not configured[/yellow]", "optional")

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


# ---------------------------------------------------------------------------
# autopsy history
# ---------------------------------------------------------------------------


def _fmt_created_at(iso: str) -> str:
    """Format ISO timestamp to `YYYY-MM-DD HH:MMZ` (best-effort)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%MZ")
    except Exception:
        return iso


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else (s[: n - 1] + "…")


@cli.group()
def history() -> None:
    """Browse past diagnoses."""


@history.command(name="list")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of results")
def history_list(limit: int) -> None:
    """Show recent diagnoses."""
    from autopsy.history import HistoryStore

    out = Console()
    with HistoryStore() as store:
        rows = store.list_recent(limit=int(limit))

    if not rows:
        out.print("No diagnoses saved yet. Run 'autopsy diagnose' first.")
        return

    table = Table(title="Diagnosis History", show_header=True, header_style="bold")
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Date", style="dim", no_wrap=True)
    table.add_column("Summary")
    table.add_column("Category", no_wrap=True)
    table.add_column("Confidence", justify="right", no_wrap=True)
    table.add_column("Repo", style="dim")
    table.add_column("Duration", justify="right", no_wrap=True)

    for idx, r in enumerate(rows, start=1):
        dur = f"{r.duration_s:.1f}s" if r.duration_s is not None else "—"
        repo = r.github_repo or "—"
        table.add_row(
            str(idx),
            r.id[:8],
            _fmt_created_at(r.created_at),
            _truncate(r.summary, 60),
            r.category,
            f"{r.confidence:.2f}",
            repo,
            dur,
        )

    out.print(table)


@history.command(name="show")
@click.argument("diagnosis_id")
@click.option(
    "--postmortem",
    "-pm",
    is_flag=True,
    help="Generate post-mortem from saved diagnosis.",
)
def history_show(diagnosis_id: str, postmortem: bool) -> None:
    """Show full details of a past diagnosis."""
    import json

    from autopsy.ai.models import DiagnosisResult
    from autopsy.history import HistoryStore
    from autopsy.renderers.terminal import TerminalRenderer
    from autopsy.utils.errors import HistoryAmbiguousMatchError, HistoryError

    out = Console()
    try:
        with HistoryStore() as store:
            row = store.get(diagnosis_id)
    except HistoryAmbiguousMatchError as exc:
        out.print(f'⚠ Multiple diagnoses match "{exc.prefix}":\n')
        for c in exc.candidates:
            short = str(c["id"])[:8]
            date = _fmt_created_at(str(c.get("created_at", "")))
            summary = _truncate(str(c.get("summary", "")), 80)
            out.print(f"  {short}  {date}  {summary}")
        out.print()
        out.print("Use the full ID: autopsy history show <id>")
        return
    except HistoryError as exc:
        out.print(f"[red]✘ {exc.message}[/red]")
        if exc.hint:
            out.print(f"[dim]{exc.hint}[/dim]")
        return

    if row is None:
        out.print(f"No diagnosis found matching {diagnosis_id}.")
        return

    created_at = _fmt_created_at(str(row.get("created_at", "")))
    duration = row.get("duration_s")
    duration_s = f"{float(duration):.1f}s" if duration is not None else "—"
    repo = row.get("github_repo") or "—"

    raw = str(row.get("raw_json") or "{}")
    try:
        result = DiagnosisResult.model_validate(json.loads(raw))
    except Exception:
        # If raw_json is corrupted, still show something rather than crashing.
        out.print("[red]✘ Saved diagnosis JSON is corrupted.[/red]")
        return

    out.print(f"Diagnosis from {created_at} ({duration_s}) — {repo}\n")
    TerminalRenderer().render(result)

    if postmortem:
        from autopsy.renderers.postmortem import PostMortemMetadata, PostMortemRenderer

        log_groups_raw = row.get("log_groups")
        try:
            log_groups = json.loads(log_groups_raw) if log_groups_raw else []
        except Exception:
            log_groups = []

        meta = PostMortemMetadata(
            provider=str(row.get("provider") or ""),
            model=str(row.get("model") or ""),
            log_groups=list(log_groups),
            time_window=int(row.get("time_window") or 0),
            duration_s=(float(duration) if duration is not None else None),
        )
        pm = PostMortemRenderer()
        markdown = pm.render(result, output_path=None, metadata=meta)
        default_path = Path(pm._generate_filename(result))
        default_path.write_text(markdown, encoding="utf-8")
        out.print(f"[green]✔ Post-mortem saved to {default_path}[/green]")


@history.command(name="search")
@click.argument("query")
@click.option("--limit", "-n", default=20, show_default=True, help="Number of results")
def history_search(query: str, limit: int) -> None:
    """Search past diagnoses."""
    from autopsy.history import HistoryStore

    out = Console()
    with HistoryStore() as store:
        rows = store.search(query, limit=int(limit))

    if not rows:
        out.print("No matches.")
        return

    table = Table(title=f"Search: {query}", show_header=True, header_style="bold")
    table.add_column("#", style="dim", no_wrap=True)
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Date", style="dim", no_wrap=True)
    table.add_column("Summary")
    table.add_column("Category", no_wrap=True)
    table.add_column("Confidence", justify="right", no_wrap=True)
    table.add_column("Repo", style="dim")
    table.add_column("Duration", justify="right", no_wrap=True)

    for idx, r in enumerate(rows, start=1):
        dur = f"{r.duration_s:.1f}s" if r.duration_s is not None else "—"
        repo = r.github_repo or "—"
        table.add_row(
            str(idx),
            r.id[:8],
            _fmt_created_at(r.created_at),
            _truncate(r.summary, 60),
            r.category,
            f"{r.confidence:.2f}",
            repo,
            dur,
        )

    out.print(table)


@history.command(name="stats")
def history_stats() -> None:
    """Show diagnosis statistics."""
    from autopsy.history import HistoryStore

    out = Console()
    with HistoryStore() as store:
        stats = store.get_stats()

    if stats["total"] == 0:
        out.print(Panel("No diagnoses saved yet.", title="History Stats", border_style="dim"))
        return

    total = int(stats["total"])
    date_min = _fmt_created_at(str(stats["date_min"]))
    date_max = _fmt_created_at(str(stats["date_max"]))
    top_cat = stats.get("top_category") or "—"
    top_cat_count = int(stats.get("top_category_count") or 0)
    top_repo = stats.get("top_repo") or "—"
    top_repo_count = int(stats.get("top_repo_count") or 0)
    avg_conf = stats.get("avg_confidence")
    avg_dur = stats.get("avg_duration_s")

    cat_pct = (top_cat_count / total * 100.0) if total else 0.0

    lines = [
        f"Total diagnoses: {total}",
        f"Date range: {date_min} → {date_max}",
        f"Most common category: {top_cat} ({cat_pct:.0f}%)",
        f"Average confidence: {(float(avg_conf) if avg_conf is not None else 0.0):.2f}"
        if avg_conf is not None
        else "Average confidence: —",
        f"Most diagnosed repo: {top_repo} ({top_repo_count})",
        f"Average diagnosis time: {(float(avg_dur) if avg_dur is not None else 0.0):.1f}s"
        if avg_dur is not None
        else "Average diagnosis time: —",
    ]
    out.print(Panel("\n".join(lines), title="History Stats", border_style="cyan"))


@history.command(name="clear")
@click.confirmation_option(prompt="Delete all diagnosis history?")
def history_clear() -> None:
    """Delete all saved diagnoses."""
    from autopsy.history import HistoryStore

    out = Console()
    with HistoryStore() as store:
        n = store.clear()
    out.print(f"Cleared {n} diagnoses.")


@history.command(name="export")
@click.argument("path", type=click.Path())
@click.option("--format", "fmt", type=click.Choice(["json", "csv"]), default="json")
def history_export(path: str, fmt: str) -> None:
    """Export history to file."""
    from autopsy.history import HistoryStore

    out = Console()
    export_path = Path(path)
    with HistoryStore() as store:
        n = store.export(export_path, fmt=fmt)
    out.print(f"Exported {n} diagnoses to {export_path}.")
