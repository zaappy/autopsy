"""Inline interactive CLI for Autopsy.

When the user runs `autopsy` with no subcommand, this module provides
an inline arrow-key menu (logo, tagline, options) and runs actions
inline with progress spinners and result panels — no full-screen takeover.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text

from autopsy import PROMPT_VERSION, __version__
from autopsy.utils.errors import AutopsyError

# Brand color: Candy Apple Red (logo, menu highlight, divider)
BRAND_RED = "#FF0800"

LOGO_ASCII = """
 █████  ██    ██ ████████  ██████  ██████  ███████ ██    ██
██   ██ ██    ██    ██    ██    ██ ██   ██ ██       ██  ██
███████ ██    ██    ██    ██    ██ ██████  ███████   ████
██   ██ ██    ██    ██    ██    ██ ██           ██    ██
██   ██  ██████     ██     ██████  ██      ███████    ██
"""

console = Console()


def render_logo() -> Text:
    """Return the AUTOPSY logo as Rich Text in Candy Apple Red."""
    return Text(LOGO_ASCII.strip(), style=BRAND_RED)


def _make_tagline() -> str:
    return f"AI-powered incident diagnosis\nv{__version__} • prompt {PROMPT_VERSION} • zero-trust"


def _run_diagnose_inline() -> None:
    """Run the diagnosis pipeline inline with Rich progress spinners."""
    from autopsy.config import load_config
    from autopsy.diagnosis import DiagnosisOrchestrator
    from autopsy.renderers.terminal import TerminalRenderer

    try:
        cfg = load_config()
        console.print("[green]✔[/green] Config loaded")

        with console.status("[bold]Running diagnosis pipeline...[/bold]"):
            orchestrator = DiagnosisOrchestrator(cfg)
            result = orchestrator.run()

        console.print("[green]✔[/green] Diagnosis complete\n")
        TerminalRenderer().render(result)

    except AutopsyError as e:
        text = Text()
        text.append(f"❌ {e.message}\n", style="bold red")
        if e.hint:
            text.append(f"\n✅ {e.hint}\n", style="green")
        console.print(text)


def run_interactive() -> None:
    """Entry point: show logo + inline arrow-key menu, loop until Quit."""
    import questionary
    from questionary import Style

    brand_style = Style(
        [
            ("selected", f"fg:{BRAND_RED} bold"),
            ("pointer", f"fg:{BRAND_RED} bold"),
            ("highlighted", f"fg:{BRAND_RED} bold"),
            ("answer", "bold"),
        ]
    )

    console.print(render_logo())
    console.print(_make_tagline(), style="dim")
    console.print()

    choices = [
        "Diagnose  — Pull logs + deploys → AI root cause",
        "Setup     — Interactive wizard (AWS, GitHub, AI)",
        "Validate  — Test credentials and connectivity",
        "Show config — Current config (secrets masked)",
        "Quit",
    ]

    while True:
        choice = questionary.select(
            "What would you like to do?",
            choices=choices,
            style=brand_style,
        ).ask()

        if choice is None or choice.startswith("Quit"):
            break
        elif choice.startswith("Diagnose"):
            console.print()
            _run_diagnose_inline()
            console.input("\n[dim]Press Enter to return to menu...[/dim]")
        elif choice.startswith("Setup"):
            from autopsy.config import init_wizard

            console.print()
            try:
                init_wizard()
            except AutopsyError as e:
                console.print(f"[bold red]❌ {e.message}[/bold red]")
                if e.hint:
                    console.print(f"[green]✅ {e.hint}[/green]")
            console.input("\n[dim]Press Enter to return to menu...[/dim]")
        elif choice.startswith("Validate"):
            from click.testing import CliRunner

            from autopsy.cli import config_validate

            console.print()
            runner = CliRunner(mix_stderr=False)
            result = runner.invoke(config_validate, [], catch_exceptions=False)
            console.print(result.output)
        elif choice.startswith("Show config"):
            from click.testing import CliRunner

            from autopsy.cli import config_show

            console.print()
            runner = CliRunner(mix_stderr=False)
            result = runner.invoke(config_show, [], catch_exceptions=False)
            console.print(result.output)

    console.print("\nGoodbye.")
