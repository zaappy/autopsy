"""Custom exception hierarchy for Autopsy.

Every user-facing error includes: what went wrong, why, and how to fix it.
All exceptions inherit from AutopsyError.
"""

from __future__ import annotations


class AutopsyError(Exception):
    """Base exception for all Autopsy errors."""

    def __init__(
        self,
        message: str,
        hint: str = "",
        docs_url: str = "",
    ) -> None:
        self.message = message
        self.hint = hint
        self.docs_url = docs_url
        super().__init__(message)


# --- Config errors ---


class ConfigError(AutopsyError):
    """Base for configuration-related errors."""


class ConfigNotFoundError(ConfigError):
    """~/.autopsy/config.yaml is missing."""


class ConfigValidationError(ConfigError):
    """Config schema or value violation."""


# --- Collector errors ---


class CollectorError(AutopsyError):
    """Base for data collector errors."""


class AWSAuthError(CollectorError):
    """IAM credentials invalid or expired."""


class AWSPermissionError(CollectorError):
    """Missing required AWS IAM permissions (e.g., logs:StartQuery)."""


class GitHubAuthError(CollectorError):
    """GitHub PAT invalid or expired."""


class GitHubRateLimitError(CollectorError):
    """GitHub API rate limit exceeded."""


class NoDataError(CollectorError):
    """Query returned zero results."""


# --- AI errors ---


class AIError(AutopsyError):
    """Base for AI provider errors."""


class AIAuthError(AIError):
    """AI provider API key invalid."""


class AIRateLimitError(AIError):
    """AI provider rate limit or quota exceeded."""


class AIResponseError(AIError):
    """Malformed JSON response from AI after retry."""


class AITimeoutError(AIError):
    """AI response exceeded timeout (60s)."""


# --- Render errors ---


class RenderError(AutopsyError):
    """Terminal or file rendering failure (typically non-fatal)."""
