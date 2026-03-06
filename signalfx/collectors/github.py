"""GitHub deploys and diffs collector.

Pulls recent commits, diffs, PR metadata, and deployment events using
PyGitHub. Implements smart diff reduction to fit LLM context budgets.
"""

from __future__ import annotations

from signalfx.collectors.base import BaseCollector, CollectedData


class GitHubCollector(BaseCollector):
    """Collects recent deploys, diffs, and PR metadata from GitHub."""

    @property
    def name(self) -> str:
        """Collector identifier."""
        return "github"

    def validate_config(self, config: dict) -> bool:
        """Verify GitHub PAT and repository access.

        Args:
            config: The 'github' section of SignalFXConfig.

        Returns:
            True if authentication and repo access succeed.

        Raises:
            GitHubAuthError: On invalid or expired PAT.
            GitHubRateLimitError: On API rate limit exceeded.
        """
        raise NotImplementedError

    def collect(self, config: dict) -> CollectedData:
        """Pull recent deploys, diffs, and PR metadata from GitHub.

        Smart diff reduction:
        - Include only: *.py, *.js, *.ts, *.go, *.java, *.yaml, *.yml,
          *.json, Dockerfile, *.tf
        - Exclude: test files, lock files, generated code
        - Cap each diff at 200 lines
        - If commit touches >10 files, keep 10 largest + summary

        Args:
            config: The 'github' section of SignalFXConfig.

        Returns:
            Normalized CollectedData with deploy entries.

        Raises:
            GitHubAuthError: On auth failure.
            GitHubRateLimitError: On rate limit.
            NoDataError: On zero results.
        """
        raise NotImplementedError
