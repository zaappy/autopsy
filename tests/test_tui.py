"""Tests for autopsy interactive inline CLI."""

from __future__ import annotations

from unittest.mock import patch

from autopsy.interactive import BRAND_RED, _make_tagline, render_logo


# ---------------------------------------------------------------------------
# Logo and tagline
# ---------------------------------------------------------------------------


def test_render_logo_returns_rich_text_in_brand_color() -> None:
    logo = render_logo()
    assert BRAND_RED in str(logo.style)
    assert "AUTOPSY" in logo.plain or "█" in logo.plain


def test_make_tagline_includes_version_and_prompt() -> None:
    tag = _make_tagline()
    assert "AI-powered" in tag
    assert "zero-trust" in tag


# ---------------------------------------------------------------------------
# CLI: no subcommand launches interactive mode
# ---------------------------------------------------------------------------


def test_cli_with_no_subcommand_calls_run_interactive() -> None:
    """With no args, cli() should call run_interactive() (mocked to avoid I/O)."""
    from click.testing import CliRunner

    from autopsy.cli import cli

    with patch("autopsy.interactive.run_interactive") as m_run:
        m_run.return_value = None
        runner = CliRunner()
        result = runner.invoke(cli, [], catch_exceptions=False)
    m_run.assert_called_once()
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# run_interactive: menu loop behaviour (questionary mocked)
# ---------------------------------------------------------------------------


def test_run_interactive_quit_exits_cleanly() -> None:
    """Selecting Quit should exit the loop without error."""
    from autopsy.interactive import run_interactive

    with patch("questionary.select") as mock_select:
        mock_q = mock_select.return_value
        mock_q.ask.return_value = "Quit"
        run_interactive()
    mock_select.assert_called_once()


def test_run_interactive_none_answer_exits_cleanly() -> None:
    """If questionary returns None (e.g. Ctrl-C), the loop exits cleanly."""
    from autopsy.interactive import run_interactive

    with patch("questionary.select") as mock_select:
        mock_q = mock_select.return_value
        mock_q.ask.return_value = None
        run_interactive()
    mock_select.assert_called_once()


def test_run_interactive_diagnose_calls_orchestrator() -> None:
    """Selecting Diagnose should invoke DiagnosisOrchestrator.run()."""
    from unittest.mock import MagicMock

    from autopsy.interactive import run_interactive

    mock_result = MagicMock()
    mock_result.correlated_deploy.commit_sha = "abc1234"

    with (
        patch("questionary.select") as mock_select,
        patch("autopsy.config.load_config") as mock_load,
        patch("autopsy.diagnosis.DiagnosisOrchestrator") as mock_orch_cls,
        patch("autopsy.renderers.terminal.TerminalRenderer.render"),
    ):
        mock_q = mock_select.return_value
        # First call: Diagnose; second call: Quit
        mock_q.ask.side_effect = [
            "Diagnose  — Pull logs + deploys → AI root cause",
            "Quit",
        ]
        mock_load.return_value = MagicMock()
        mock_orch = mock_orch_cls.return_value
        mock_orch.run.return_value = mock_result

        run_interactive()

    mock_orch.run.assert_called_once()


def test_run_interactive_setup_calls_init_wizard() -> None:
    """Selecting Setup should call init_wizard()."""
    from autopsy.interactive import run_interactive

    with (
        patch("questionary.select") as mock_select,
        patch("autopsy.config.init_wizard") as mock_wizard,
    ):
        mock_q = mock_select.return_value
        mock_q.ask.side_effect = [
            "Setup     — Interactive wizard (AWS, GitHub, AI)",
            "Quit",
        ]
        mock_wizard.return_value = None
        run_interactive()

    mock_wizard.assert_called_once()
