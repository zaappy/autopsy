# Contributing to Autopsy CLI

Thank you for your interest in contributing. This document explains how to get set up and submit changes.

## Development setup

1. **Fork and clone**
   ```bash
   git clone https://github.com/YOUR_USERNAME/autopsy.git && cd autopsy
   ```

2. **Create a virtual environment** (recommended)
   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   ```

3. **Install in editable mode with dev dependencies**
   ```bash
   pip install -e ".[dev]"
   ```

4. **Configure** (optional, for running diagnose locally)
   ```bash
   autopsy init
   ```

## Code style and quality

- **Linting:** We use [Ruff](https://docs.astral.sh/ruff/). Run before committing:
  ```bash
  ruff check .
  ```
- **Type checking:** We use mypy in strict mode:
  ```bash
  mypy autopsy
  ```
- **Tests:** Use pytest. All tests must pass:
  ```bash
  pytest -v
  ```

CI runs `ruff check .` and `pytest` on every push and pull request.

## Project layout

- **`autopsy/cli.py`** — CLI entrypoint and subcommands only; no business logic.
- **`autopsy/collectors/`** — Data collectors (CloudWatch, Datadog, GitHub, GitLab).
- **`autopsy/ai/`** — AI engine (prompts, models, diagnosis).
- **`autopsy/renderers/`** — Terminal and JSON output.
- **`autopsy/config.py`** — Configuration loading and validation.
- **`tests/`** — Pytest tests (including VCR for HTTP).

Please keep this separation: collectors, AI, and renderers are independent of the CLI layer.

## Submitting changes

1. Create a branch from `main` (e.g. `fix/cloudwatch-timeout`, `feat/add-datadog`).
2. Make your changes and add or update tests as needed.
3. Run `ruff check .` and `pytest` locally.
4. Commit with clear messages (e.g. "Fix CloudWatch log group pagination").
5. Push to your fork and open a pull request against `main`.
6. Fill out the PR template. A maintainer will review when possible.

## Reporting issues

Use [GitHub Issues](https://github.com/zaappy/autopsy/issues). For bugs, use the bug report template and include:

- OS and Python version
- Steps to reproduce
- Expected vs actual behavior
- Relevant config (with secrets removed)

For security concerns, see [SECURITY.md](SECURITY.md).

## Questions

Open a [Discussion](https://github.com/zaappy/autopsy/discussions) or an issue and we’ll try to help.

Thanks for contributing.
