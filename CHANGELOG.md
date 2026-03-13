# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Datadog log collector: optional integration with Datadog Logs API (validate + search); supports us1/eu1/us3/us5/ap1; API/App keys via env; skips with warning when keys are missing.
- Shared log reduction pipeline (dedup, truncation, token budget) used by CloudWatch and Datadog.
- Init wizard can add optional Datadog config (site, service, source) and write `DD_API_KEY` / `DD_APP_KEY` to `~/.autopsy/.env`.
- `autopsy config validate` checks Datadog when configured.

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
