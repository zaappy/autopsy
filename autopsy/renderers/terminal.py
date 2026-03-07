"""Rich terminal renderer for diagnosis output.

Produces Slack-pasteable panels: Root Cause, Correlated Deploy,
Suggested Fix, and Timeline. Clean when copied as plain text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.table import Table

from autopsy.ai.models import DiagnosisResult  # noqa: TC001 — used at runtime for render()

console = Console()


def _confidence_style(confidence: float) -> str:
    """Return Rich style for confidence level (red < 0.5, yellow 0.5–0.75, green > 0.75)."""
    if confidence < 0.5:
        return "red"
    if confidence <= 0.75:
        return "yellow"
    return "green"


def _category_style(category: str) -> str:
    """Return Rich style for category tag."""
    styles = {
        "code_change": "bold blue",
        "config_change": "bold magenta",
        "infra": "bold cyan",
        "dependency": "bold yellow",
        "traffic": "bold white",
        "unknown": "dim",
    }
    return styles.get(category, "dim")


class BaseRenderer(ABC):
    """Abstract renderer interface."""

    @abstractmethod
    def render(self, result: DiagnosisResult) -> None:
        """Output the diagnosis result.

        Args:
            result: Structured diagnosis from the AI engine.
        """


class TerminalRenderer(BaseRenderer):
    """Renders DiagnosisResult as Rich panels to stdout.

    Four panels: Root Cause (with confidence bar), Correlated Deploy,
    Suggested Fix, Timeline table. Design is Slack-pasteable.
    """

    def render(self, result: DiagnosisResult) -> None:
        """Render diagnosis as colored Rich panels.

        Args:
            result: Structured diagnosis from the AI engine.
        """
        # Panel 1: Root Cause — summary, category tag, confidence bar, evidence bullets
        rc = result.root_cause
        conf_style = _confidence_style(rc.confidence)
        cat_style = _category_style(rc.category)

        root_lines: list[str] = []
        root_lines.append(rc.summary)
        root_lines.append("")
        root_lines.append(f"Category: [{cat_style}]{rc.category}[/]")
        root_lines.append(f"Confidence: [{conf_style}]{rc.confidence:.0%}[/]")
        # Confidence bar (10 chars)
        filled = int(rc.confidence * 10)
        bar = "█" * filled + "░" * (10 - filled)
        root_lines.append(f"[{conf_style}]{bar}[/]")
        if rc.evidence:
            root_lines.append("")
            root_lines.append("Evidence:")
            for line in rc.evidence:
                root_lines.append(f"  • {line}")

        console.print(
            Panel(
                "\n".join(root_lines),
                title="[bold]Root Cause[/bold]",
                border_style="blue",
                padding=(0, 1),
            )
        )

        # Panel 2: Correlated Deploy
        cd = result.correlated_deploy
        deploy_lines: list[str] = []
        if cd.commit_sha:
            deploy_lines.append(f"Commit: {cd.commit_sha}")
        if cd.author:
            deploy_lines.append(f"Author: {cd.author}")
        if cd.pr_title:
            deploy_lines.append(f"PR: {cd.pr_title}")
        if cd.changed_files:
            deploy_lines.append("Changed files:")
            for f in cd.changed_files:
                deploy_lines.append(f"  • {f}")
        if not deploy_lines:
            deploy_lines.append("No deploy correlated.")

        console.print(
            Panel(
                "\n".join(deploy_lines),
                title="[bold]Correlated Deploy[/bold]",
                border_style="cyan",
                padding=(0, 1),
            )
        )

        # Panel 3: Suggested Fix
        sf = result.suggested_fix
        fix_lines = [
            "[bold]Immediate:[/bold]",
            sf.immediate,
            "",
            "[bold]Long-term:[/bold]",
            sf.long_term,
        ]
        console.print(
            Panel(
                "\n".join(fix_lines),
                title="[bold]Suggested Fix[/bold]",
                border_style="green",
                padding=(0, 1),
            )
        )

        # Panel 4: Timeline table
        table = Table(
            title="Timeline",
            show_header=True,
            header_style="bold",
            border_style="white",
            box=None,
        )
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Event")
        for ev in result.timeline:
            table.add_row(ev.time, ev.event)
        if not result.timeline:
            table.add_row("—", "No events.")
        console.print(Panel(table, border_style="white", padding=(0, 1)))

        if result.raw_response:
            console.print(
                Panel(
                    result.raw_response,
                    title="[dim]Raw AI response (parse fallback)[/dim]",
                    border_style="dim",
                    padding=(0, 1),
                )
            )

    def get_renderables(self, result: DiagnosisResult) -> list[RenderableType]:
        """Return the same panels as Rich renderables (for TUI embedding).

        Args:
            result: Structured diagnosis from the AI engine.

        Returns:
            List of Rich renderables (Panels, Table in Panel) in display order.
        """
        out: list[RenderableType] = []
        rc = result.root_cause
        conf_style = _confidence_style(rc.confidence)
        cat_style = _category_style(rc.category)
        root_lines: list[str] = []
        root_lines.append(rc.summary)
        root_lines.append("")
        root_lines.append(f"Category: [{cat_style}]{rc.category}[/]")
        root_lines.append(f"Confidence: [{conf_style}]{rc.confidence:.0%}[/]")
        filled = int(rc.confidence * 10)
        bar = "█" * filled + "░" * (10 - filled)
        root_lines.append(f"[{conf_style}]{bar}[/]")
        if rc.evidence:
            root_lines.append("")
            root_lines.append("Evidence:")
            for line in rc.evidence:
                root_lines.append(f"  • {line}")
        out.append(
            Panel(
                "\n".join(root_lines),
                title="[bold]Root Cause[/bold]",
                border_style="blue",
                padding=(0, 1),
            )
        )
        cd = result.correlated_deploy
        deploy_lines = []
        if cd.commit_sha:
            deploy_lines.append(f"Commit: {cd.commit_sha}")
        if cd.author:
            deploy_lines.append(f"Author: {cd.author}")
        if cd.pr_title:
            deploy_lines.append(f"PR: {cd.pr_title}")
        if cd.changed_files:
            deploy_lines.append("Changed files:")
            for f in cd.changed_files:
                deploy_lines.append(f"  • {f}")
        if not deploy_lines:
            deploy_lines.append("No deploy correlated.")
        out.append(
            Panel(
                "\n".join(deploy_lines),
                title="[bold]Correlated Deploy[/bold]",
                border_style="cyan",
                padding=(0, 1),
            )
        )
        sf = result.suggested_fix
        fix_lines = [
            "[bold]Immediate:[/bold]",
            sf.immediate,
            "",
            "[bold]Long-term:[/bold]",
            sf.long_term,
        ]
        out.append(
            Panel(
                "\n".join(fix_lines),
                title="[bold]Suggested Fix[/bold]",
                border_style="green",
                padding=(0, 1),
            )
        )
        table = Table(
            title="Timeline",
            show_header=True,
            header_style="bold",
            border_style="white",
            box=None,
        )
        table.add_column("Time", style="dim", no_wrap=True)
        table.add_column("Event")
        for ev in result.timeline:
            table.add_row(ev.time, ev.event)
        if not result.timeline:
            table.add_row("—", "No events.")
        out.append(Panel(table, border_style="white", padding=(0, 1)))
        if result.raw_response:
            out.append(
                Panel(
                    result.raw_response,
                    title="[dim]Raw AI response (parse fallback)[/dim]",
                    border_style="dim",
                    padding=(0, 1),
                )
            )
        return out
