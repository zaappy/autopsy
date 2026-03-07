"""Tests for autopsy TUI — interactive mode and CLI wiring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from autopsy.tui import BRAND_RED, render_logo, _make_tagline


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
# AutopsyApp initializes (requires textual)
# ---------------------------------------------------------------------------


def test_autopsy_app_initializes_without_error() -> None:
    """Test that AutopsyApp can be constructed (textual must be installed)."""
    from autopsy.tui import _import_app

    AppClass = _import_app()
    app = AppClass()
    assert app is not None


# ---------------------------------------------------------------------------
# CLI: no subcommand launches TUI
# ---------------------------------------------------------------------------


def test_cli_with_no_subcommand_calls_run_tui() -> None:
    """With no args, cli() should call run_tui() (we mock it to avoid starting the app)."""
    from click.testing import CliRunner

    from autopsy.cli import cli

    with patch("autopsy.tui.run_tui") as m_run_tui:
        m_run_tui.return_value = 0
        runner = CliRunner()
        result = runner.invoke(cli, [], catch_exceptions=False)
    m_run_tui.assert_called_once()
    assert result.exit_code == 0


def test_cli_fallback_to_help_when_tui_import_fails() -> None:
    """When run_tui raises ImportError (e.g. textual not installed), print help."""
    from click.testing import CliRunner

    from autopsy.cli import cli

    with patch("autopsy.tui.run_tui", side_effect=ImportError("No module named 'textual'")):
        runner = CliRunner()
        result = runner.invoke(cli, [], catch_exceptions=False)
    assert result.exit_code == 0
    assert "Autopsy" in result.output or "autopsy" in result.output
    assert "diagnose" in result.output or "init" in result.output


# ---------------------------------------------------------------------------
# TUI exit codes invoke correct commands
# ---------------------------------------------------------------------------


def test_cli_invokes_init_when_tui_returns_100() -> None:
    """TUI exit code 100 should run the init path (init_wizard called)."""
    from pathlib import Path

    from click.testing import CliRunner

    from autopsy.cli import cli

    with patch("autopsy.tui.run_tui", return_value=100):
        with patch("autopsy.config.init_wizard") as m_init_wizard:
            m_init_wizard.return_value = Path.home() / ".autopsy" / "config.yaml"
            runner = CliRunner()
            runner.invoke(cli, [], catch_exceptions=False)
    m_init_wizard.assert_called_once()


def test_cli_invokes_validate_when_tui_returns_101() -> None:
    """TUI exit code 101 should run the validate path (validate_config called)."""
    from unittest.mock import MagicMock

    from click.testing import CliRunner

    from autopsy.cli import cli

    with patch("autopsy.tui.run_tui", return_value=101):
        with patch("autopsy.config.load_config") as m_load:
            with patch("autopsy.config.validate_config") as m_validate:
                m_load.return_value = MagicMock()
                m_validate.return_value = {}
                runner = CliRunner()
                runner.invoke(cli, [], catch_exceptions=False)
    m_validate.assert_called_once()


def test_cli_invokes_config_show_when_tui_returns_102() -> None:
    """TUI exit code 102 should run the config show path (load_config called)."""
    from click.testing import CliRunner

    from autopsy.cli import cli
    from autopsy.config import AIConfig, AutopsyConfig, AWSConfig, GitHubConfig

    minimal_cfg = AutopsyConfig(
        aws=AWSConfig(region="us-east-1", log_groups=["/x"]),
        github=GitHubConfig(repo="a/b"),
        ai=AIConfig(),
    )
    with patch("autopsy.tui.run_tui", return_value=102):
        with patch("autopsy.config.load_config") as m_load:
            m_load.return_value = minimal_cfg
            runner = CliRunner()
            runner.invoke(cli, [], catch_exceptions=False)
    m_load.assert_called_once()


# ---------------------------------------------------------------------------
# Textual run_test (async)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_run_test_menu_visible() -> None:
    """Run the app with run_test(); menu and logo should be present."""
    from autopsy.tui import _import_app

    AppClass = _import_app()
    app = AppClass()
    async with app.run_test() as pilot:
        await pilot.pause()
        # Default view is menu; logo and menu are mounted
        logo = app.query_one("#logo")
        menu = app.query_one("#menu")
        assert logo is not None
        assert menu is not None
