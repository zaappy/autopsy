# Autopsy CLI

<pre style="color: #FF0800; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 14px; line-height: 1.2; margin: 0;">
 █████  ██    ██ ████████  ██████  ██████  ███████ ██    ██
██   ██ ██    ██    ██    ██    ██ ██   ██ ██       ██  ██
███████ ██    ██    ██    ██    ██ ██████  ███████   ████
██   ██ ██    ██    ██    ██    ██ ██           ██    ██
██   ██  ██████     ██     ██████  ██      ███████    ██
</pre>
*AI-powered incident diagnosis • zero-trust*

[![CI](https://github.com/zaappy/autopsy/actions/workflows/ci.yml/badge.svg)](https://github.com/zaappy/autopsy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/autopsy-cli.svg)](https://pypi.org/project/autopsy-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**AI-powered incident diagnosis for engineering teams.** Pull production error logs and recent deploys, send them to an LLM, and get a structured root cause analysis in the terminal in under a minute. Zero-trust: your data never leaves your environment.

## Demo

Watch a short walkthrough of the TUI and diagnosis flow:

https://github.com/user-attachments/assets/78ed3c52-1fe1-4ae2-844b-e0ed1848c06c

## Prerequisites

Before running Autopsy, you need:

| Area | Requirement |
|------|-------------|
| **AWS** | Account with CloudWatch Logs; credentials via `aws configure` or `AWS_PROFILE`; IAM: `logs:DescribeLogGroups`, `logs:StartQuery`, `logs:GetQueryResults`; at least one log group. *Check:* `aws sts get-caller-identity` returns your account ID. |
| **GitHub** | Account + [Personal Access Token](https://github.com/settings/tokens) with **`repo`** scope; your app’s repo on GitHub. *Check:* `curl -H "Authorization: Bearer YOUR_TOKEN" https://api.github.com/user` returns your username. |
| **AI provider** | **OpenAI** ([platform.openai.com](https://platform.openai.com)) or **Anthropic** ([console.anthropic.com](https://console.anthropic.com)) — account, API key, and credits. |
| **Local** | Python 3.10+ (`python --version`), pip, terminal, internet. |

**Quick checklist:** `python --version` → 3.10+ · `aws sts get-caller-identity` → OK · GitHub PAT with `repo` · OpenAI or Anthropic API key · `pip install autopsy-cli` · `autopsy init` · `autopsy diagnose`.

**You do not need:** a server, Docker, a separate cloud account for Autopsy, a database, or admin/root; no changes to your AWS or app code.

## Install

```bash
pip install autopsy-cli          # core CLI (no TUI)
pip install "autopsy-cli[tui]"   # + interactive terminal UI (requires textual)
```

Or from source:

```bash
git clone https://github.com/zaappy/autopsy.git && cd autopsy
pip install -e ".[dev]"
```

## Quick Start

```bash
autopsy           # Launch interactive TUI (menu, then run Diagnose or Setup)
autopsy init      # Or: interactive config wizard (~/.autopsy/config.yaml)
autopsy diagnose  # Or: run diagnosis directly (CloudWatch + GitHub → AI → panels)
```

**Interactive TUI** — Run `autopsy` with no arguments to open the interactive terminal UI:

- **AUTOPSY** logo and tagline (*AI-powered incident diagnosis • zero-trust*)
- Arrow-key menu: **Diagnose**, **Setup configuration**, **Validate connections**, **Show config**, **Diagnosis history** (cloud, disabled)
- Shortcuts: `d` Diagnose, `i` Init, `v` Validate, `c` Config, `q` Quit, `Esc` Back
- Choosing **Diagnose** runs the full pipeline inside the TUI (progress steps, then 4-panel result). Errors are shown inline; press `Esc` to return to the menu.
- Choosing **Setup** / **Validate** / **Show config** exits the TUI and runs the corresponding CLI command in your terminal.

Three steps: **install → init → diagnose** (via TUI or direct commands).

## Configuration

After `autopsy init`, edit `~/.autopsy/config.yaml` or re-run the wizard. The init wizard stores credentials in `~/.autopsy/.env` — no manual env var exports needed.

| Section   | Purpose |
|----------|---------|
| **aws**  | CloudWatch region, log groups, time window (minutes). Uses your AWS CLI credentials. |
| **github** | Repo (`owner/repo`), branch, number of recent commits to analyze. Uses `GITHUB_TOKEN`. |
| **ai**   | Provider (`anthropic` or `openai`), model, API keys. |

Credentials are loaded from `~/.autopsy/.env` automatically. If you prefer env vars, export them in your shell — they take precedence over the `.env` file.

**Security:** Add `~/.autopsy/.env` to `.gitignore` if you ever copy the config directory. Never commit credentials. If your home directory is backed up or synced (e.g. OneDrive, Time Machine, Google Drive), the `.env` file may be included — consider excluding `~/.autopsy/` from sync or use env vars instead.

```bash
autopsy config show       # Print config (secrets masked)
autopsy config validate   # Check env vars and connectivity
```

## How It Works

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐     ┌──────────────┐
│   Config    │────▶│  Data Collectors │────▶│  AI Engine  │────▶│  Renderers   │
│ ~/.autopsy  │     │  CloudWatch      │     │  (Claude /   │     │  Terminal or │
│ config.yaml │     │  GitHub          │     │   OpenAI)    │     │  JSON        │
└─────────────┘     └──────────────────┘     └─────────────┘     └──────────────┘
                           │                          │
                           ▼                          ▼
                    Logs + recent commits      Structured diagnosis:
                    (deduped, truncated)       root cause, deploy, fix, timeline
```

1. **Collect** — CloudWatch Logs Insights (error-level) and GitHub (last N commits + diffs).
2. **Reduce** — Log dedup and token budget; diff filters (code files only, cap per file).
3. **Diagnose** — Single prompt with logs + deploys; LLM returns JSON (root cause, correlated deploy, suggested fix, timeline).
4. **Render** — Rich panels in the terminal or `--json` for piping.

## CLI Reference

| Command | Description |
|--------|-------------|
| `autopsy` | **Interactive TUI** — menu with Diagnose, Setup, Validate, Config (requires `textual`) |
| `autopsy init` | Interactive config wizard |
| `autopsy diagnose` | Run full diagnosis pipeline (same as TUI “Diagnose”) |
| `autopsy diagnose --json` | Output raw JSON |
| `autopsy diagnose --time-window 15` | Override log window (minutes) |
| `autopsy diagnose --log-group /aws/lambda/foo` | Override log groups (repeatable) |
| `autopsy diagnose --provider openai` | Use OpenAI instead of Anthropic |
| `autopsy config show` | Print config (secrets masked) |
| `autopsy config validate` | Check credentials and connectivity |
| `autopsy version` / `autopsy --version` | CLI version, prompt version, Python version |

If `textual` is not installed, `autopsy` with no arguments prints help instead of starting the TUI.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, code style, and how to submit changes. In short:

1. Fork the repo and create a branch.
2. Install dev deps: `pip install -e ".[dev]"`.
3. Run lint and tests: `ruff check . && pytest`.
4. Open a PR against `main`.

We follow the layout and conventions in the repo (collectors, AI engine, renderers, no business logic in `cli.py`).

## License

MIT. See [LICENSE](LICENSE).
