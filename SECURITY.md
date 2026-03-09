# Security Policy

## Supported Versions

We release security updates for the current minor version and the previous one when feasible. Please try to stay on a recent release.

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | :white_check_mark: |
| < 0.2   | :x:                |

## Reporting a Vulnerability

If you believe you’ve found a security vulnerability in Autopsy CLI, please report it responsibly.

**Do not open a public GitHub issue for security-sensitive bugs.**

- **Email:** Report to zaappy@users.noreply.github.com with a clear description of the issue, steps to reproduce, and impact if possible.
- We’ll acknowledge receipt and will follow up with next steps (e.g. fix timeline, disclosure).
- We ask for reasonable time to address the issue before any public disclosure.

**What we care about:**

- Credential or token leakage (e.g. via config, env, or logs)
- Insecure handling of `~/.autopsy/.env` or API keys
- Issues in data sent to or received from third-party APIs (AWS, GitHub, AI providers)
- Any behavior that could expose user or deployment data

We appreciate your help in keeping Autopsy CLI safe for everyone.
