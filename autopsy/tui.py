"""Interactive TUI for Autopsy CLI.

When the user runs `autopsy` with no subcommand, this module provides
a Textual-based interactive menu (logo, tagline, options) and runs
diagnosis inline with progress and result panels.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.text import Text

from autopsy import PROMPT_VERSION, __version__
from autopsy.utils.errors import AutopsyError

if TYPE_CHECKING:
    from autopsy.ai.models import DiagnosisResult  # noqa: TC001

# Brand color: Candy Apple Red (logo, menu highlight, divider)
BRAND_RED = "#FF0800"

LOGO_ASCII = """
 █████  ██    ██ ████████  ██████  ██████  ███████ ██    ██
██   ██ ██    ██    ██    ██    ██ ██   ██ ██       ██  ██
███████ ██    ██    ██    ██    ██ ██████  ███████   ████
██   ██ ██    ██    ██    ██    ██ ██           ██    ██
██   ██  ██████     ██     ██████  ██      ███████    ██
"""


def render_logo() -> Text:
    """Return the AUTOPSY logo as Rich Text in Candy Apple Red."""
    return Text(LOGO_ASCII.strip(), style=BRAND_RED)


def _make_tagline() -> str:
    return f"AI-powered incident diagnosis\nv{__version__} • prompt {PROMPT_VERSION} • zero-trust"


def _run_diagnosis_sync(
    time_window: int | None = None,
    log_groups: list[str] | None = None,
    provider: str | None = None,
) -> DiagnosisResult:
    """Run diagnosis pipeline in a thread (blocking). Returns result or raises AutopsyError."""
    from autopsy.config import load_config
    from autopsy.diagnosis import DiagnosisOrchestrator

    cfg = load_config()
    orchestrator = DiagnosisOrchestrator(cfg)
    return orchestrator.run(
        time_window=time_window,
        log_groups=log_groups,
        provider=provider,
    )


def _diagnosis_progress_lines(
    *,
    config_loaded: bool = False,
    logs_done: bool = False,
    log_summary: str = "",
    deploys_done: bool = False,
    deploy_summary: str = "",
    ai_done: bool = False,
    running_step: str = "",
) -> list[str]:
    """Build progress checklist lines for the diagnose flow."""
    lines: list[str] = []
    check = "✔"
    spin = "⏳"
    if config_loaded:
        lines.append(f"{check} Config loaded")
    else:
        lines.append(f"{spin} Loading config..." if running_step == "config" else "  Config loaded")

    if logs_done:
        lines.append(f"{check} {log_summary or 'Logs collected'}")
    else:
        if running_step == "logs":
            lines.append(f"{spin} Pulling logs from log groups...")
        else:
            lines.append("  Log entries → patterns (deduped)")

    if deploys_done:
        lines.append(f"{check} {deploy_summary or 'Deploys loaded'}")
    else:
        if running_step == "deploys":
            lines.append(f"{spin} Pulling last deploys...")
        else:
            lines.append("  Deploys with diffs loaded")

    if ai_done:
        lines.append(f"{check} Diagnosis complete")
    else:
        if running_step == "ai":
            lines.append(f"{spin} Analyzing with AI...")
        else:
            lines.append("  Diagnosis complete")
    return lines


def _format_progress_done(
    log_summary: str,
    deploy_summary: str,
) -> list[str]:
    """Full checklist after diagnosis completes (all ✔)."""
    return [
        "✔ Config loaded",
        f"✔ {log_summary}",
        f"✔ {deploy_summary}",
        "✔ Diagnosis complete",
    ]


def _log_summary_from_result(result: DiagnosisResult) -> str:
    """Derive a short log summary from collected data (we don't have it on result)."""
    return "Logs collected → patterns (deduped)"


def _deploy_summary_from_result(result: DiagnosisResult) -> str:
    """Derive a short deploy summary from result."""
    cd = result.correlated_deploy
    if cd.commit_sha:
        return f"Deploys loaded (e.g. {cd.commit_sha[:7]})"
    return "Deploys loaded"


# Lazy import Textual so we can catch ImportError in cli.py
def _import_app() -> type:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, ScrollableContainer
    from textual.widgets import Footer, OptionList, Static
    from textual.widgets.option_list import Option

    class AutopsyApp(App[None]):
        """Interactive TUI for Autopsy CLI."""

        CSS = """
        Screen {
            background: #0d1117;
            align: center middle;
        }
        #welcome {
            width: 72;
            height: auto;
            padding: 2 2;
            align: center middle;
        }
        #logo {
            text-align: center;
            margin: 0 0 1 0;
        }
        #tagline {
            text-align: center;
            color: #8b949e;
            margin: 0 0 2 0;
        }
        #main-content {
            width: 100%;
            height: auto;
            padding: 0 2;
        }
        #menu {
            background: transparent;
            padding: 0 2;
            min-height: 12;
        }
        #menu .option-list--option {
            padding: 0 1;
        }
        OptionList > .option-list--option-highlighted {
            background: #FF080012;
            border-left: solid #FF0800;
        }
        #progress-view, #result-view, #error-view {
            padding: 1 2;
            width: 100%;
        }
        #result-scroll {
            width: 100%;
            height: auto;
            max-height: 60;
        }
        .back-hint {
            color: #8b949e;
            margin-top: 1;
        }
        Footer {
            dock: bottom;
            padding: 0 1;
            color: #8b949e;
        }
        """

        BINDINGS = [
            Binding("q", "quit", "Quit", show=True),
            Binding("d", "diagnose", "Diagnose", show=False),
            Binding("i", "init", "Init", show=False),
            Binding("v", "validate", "Validate", show=False),
            Binding("c", "config", "Config", show=False),
            Binding("escape", "back", "Back", show=True),
        ]
        TITLE = "Autopsy"

        def __init__(self) -> None:
            super().__init__()
            self._view: str = "menu"  # menu | progress | result | error
            self._menu_instance: OptionList | None = None

        def compose(self) -> ComposeResult:
            with Container(id="welcome"):
                yield Static(render_logo(), id="logo")
                yield Static(_make_tagline(), id="tagline")
                with Container(id="main-content"):
                    yield self._compose_menu()
            yield Footer()

        def _compose_menu(self) -> OptionList:
            self._menu_instance = OptionList(
                Option(
                    "[bold]Diagnose[/bold]  — Pull logs + deploys → AI root cause  [dim]d[/dim]",
                    id="diagnose",
                ),
                Option(
                    "[bold]Setup[/bold]  — Interactive wizard (AWS, GitHub, AI)  [dim]i[/dim]",
                    id="init",
                ),
                Option(
                    "[bold]Validate[/bold]  — Test credentials and connectivity  [dim]v[/dim]",
                    id="validate",
                ),
                Option(
                    "[bold]Show config[/bold]  — Current config (secrets masked)  [dim]c[/dim]",
                    id="config",
                ),
                Option(
                    "[bold]History[/bold]  [dim]cloud[/dim]",
                    id="history",
                    disabled=True,
                ),
                id="menu",
                markup=True,
            )
            return self._menu_instance

        def on_mount(self) -> None:
            self._view = "menu"

        def _show_menu(self) -> None:
            mc = self.query_one("#main-content", Container)
            mc.remove_children()
            self._menu_instance = self._compose_menu()
            mc.mount(self._menu_instance)
            self._menu_instance.focus()
            self._view = "menu"

        def _show_progress(self, message: str) -> None:
            mc = self.query_one("#main-content", Container)
            mc.remove_children()
            content = Static(
                f"⏳ {message}\n\n[dim]Running diagnosis pipeline...[/dim]",
                id="progress-view",
            )
            mc.mount(content)
            self._view = "progress"

        def _show_progress_done(self, lines: list[str]) -> None:
            mc = self.query_one("#main-content", Container)
            mc.remove_children()
            content = Static("\n".join(lines), id="progress-view")
            mc.mount(content)
            self._view = "progress"

        def _show_result(self, renderables: list[RenderableType]) -> None:
            mc = self.query_one("#main-content", Container)
            mc.mount(ScrollableContainer(Static(Group(*renderables), id="result-panels"), id="result-scroll"))
            back = Static("[dim]Press Esc to return to menu[/dim]", classes="back-hint")
            mc.mount(back)
            self._view = "result"

        def _show_error(self, message: str, hint: str = "") -> None:
            mc = self.query_one("#main-content", Container)
            try:
                for child in list(mc.children):
                    child.remove()
            except Exception:
                pass
            parts = [f"[bold red]❌ {message}[/]"]
            if hint:
                parts.append(f"\n[green]✅ {hint}[/]")
            parts.append("\n\n[dim]Press Esc to return to menu[/dim]")
            content = Static("\n".join(parts), id="error-view")
            mc.mount(content)
            self._view = "error"

        def _replace_main_with_result(self, renderables: list[RenderableType]) -> None:
            mc = self.query_one("#main-content", Container)
            mc.remove_children()
            scroll = ScrollableContainer(id="result-scroll")
            scroll.mount(Static(Group(*renderables), id="result-panels"))
            mc.mount(scroll)
            mc.mount(Static("[dim]Press Esc to return to menu[/dim]", classes="back-hint"))
            self._view = "result"

        def _replace_main_with_error(self, message: str, hint: str = "") -> None:
            mc = self.query_one("#main-content", Container)
            mc.remove_children()
            parts = [f"[bold red]❌ {message}[/]"]
            if hint:
                parts.append(f"\n[green]✅ {hint}[/]")
            parts.append("\n\n[dim]Press Esc to return to menu[/dim]")
            mc.mount(Static("\n".join(parts), id="error-view"))
            self._view = "error"

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            idx = event.option_index
            if idx == 0:
                self.action_diagnose()
            elif idx == 1:
                self.action_init()
            elif idx == 2:
                self.action_validate()
            elif idx == 3:
                self.action_config()
            # idx == 4 is history (disabled)

        def action_diagnose(self) -> None:
            self._run_diagnose()

        def action_init(self) -> None:
            # Exit with code 100 so cli can invoke `autopsy init`
            self.exit(100)

        def action_validate(self) -> None:
            # Exit with code 101 so cli can invoke `autopsy config validate`
            self.exit(101)

        def action_config(self) -> None:
            # Exit with code 102 so cli can invoke `autopsy config show`
            self.exit(102)

        def action_quit(self) -> None:
            self.exit()

        def action_back(self) -> None:
            if self._view in ("result", "error", "progress"):
                self._show_menu()
            else:
                self._show_menu()

        async def _run_diagnose(self) -> None:
            mc = self.query_one("#main-content", Container)
            for child in list(mc.children):
                child.remove()
            progress_static = Static("⏳ Loading config...", id="progress-view")
            mc.mount(progress_static)
            self._view = "progress"

            try:
                progress_static.update("⏳ Loading config...")
                result = await asyncio.to_thread(_run_diagnosis_sync)
            except AutopsyError as e:
                self._replace_main_with_error(e.message, e.hint or "")
                return

            from autopsy.renderers.terminal import TerminalRenderer

            log_summary = _log_summary_from_result(result)
            deploy_summary = _deploy_summary_from_result(result)
            progress_lines = _format_progress_done(log_summary, deploy_summary)
            progress_static.update("\n".join(progress_lines))
            await asyncio.sleep(0.8)

            renderer = TerminalRenderer()
            renderables = renderer.get_renderables(result)
            self._replace_main_with_result(renderables)

    return AutopsyApp


def run_tui() -> int:
    """Entry point: run the Textual Autopsy app. Returns exit code from app.run()."""
    AppClass = _import_app()
    app = AppClass()
    return app.run()
