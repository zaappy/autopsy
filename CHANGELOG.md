# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- GitLab collector: optional integration with GitLab API via `python-gitlab`; pulls commits, diffs, merge requests, and deployment events; supports self-hosted GitLab URLs; smart diff reduction matching GitHub collector; `GITLAB_TOKEN` via env; skips with warning when token is missing; can coexist with GitHub collector.
- Init wizard can add optional GitLab config (URL, project ID, branch, deploy count) and write `GITLAB_TOKEN` to `~/.autopsy/.env`.
- `autopsy config validate` checks GitLab when configured.
- AI prompts label deploy entries by source (`[Github]` / `[Gitlab]`) for multi-source correlation.
- Datadog log collector: optional integration with Datadog Logs API (validate + search); supports us1/eu1/us3/us5/ap1; API/App keys via env; skips with warning when keys are missing.
- Shared log reduction pipeline (dedup, truncation, token budget) used by CloudWatch and Datadog.
- Init wizard can add optional Datadog config (site, service, source) and write `DD_API_KEY` / `DD_APP_KEY` to `~/.autopsy/.env`.
- `autopsy config validate` checks Datadog when configured.
- Parallel collector execution: all data collectors now run concurrently by default using `asyncio.to_thread`, with `ThreadPoolExecutor` fallback for nested event loops. Partial failures are handled gracefully (warn and continue with remaining sources). Per-collector timing tracked and displayed.
- `autopsy diagnose --sequential` flag to force sequential collection for debugging.

## [0.2.2] - 2026-03-10

### Added

- Diagnosis history retention with local SQLite storage and CLI commands:
  `autopsy history list|show|search|stats|clear|export`.
- Markdown post-mortem generation via `autopsy diagnose --postmortem` and
  `autopsy history show <id> --postmortem`.
- Slack webhook output via `autopsy diagnose --slack`.
- Slack-only setup flow via `autopsy init --slack`.

### Changed

- `autopsy config validate` now includes optional Slack configuration status.
- Interactive menu labels now include Slack in setup/validation descriptions.
- Runtime package version alignment for CLI and renderer outputs.

## [0.2.1] - 2026-03-09

### Changed

- Maintenance and release updates.

## [0.2.0] - (earlier)

### Added

- Interactive TUI with menu: Diagnose, Setup, Validate, Config.
- `autopsy init` config wizard writing `~/.autopsy/config.yaml` and `~/.autopsy/.env`.
- CloudWatch Logs Insights and GitHub collectors.
- AI diagnosis engine (Anthropic / OpenAI) with structured JSON output.
- Terminal and JSON renderers.
- CLI commands: `autopsy`, `autopsy diagnose`, `autopsy config show`, `autopsy config validate`.

---

[Unreleased]: https://github.com/zaappy/autopsy/compare/main...HEAD
[0.2.2]: https://github.com/zaappy/autopsy/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/zaappy/autopsy/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/zaappy/autopsy/releases/tag/v0.2.0
