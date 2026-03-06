"""Custom exception hierarchy for SignalFX.

Every user-facing error includes: what went wrong, why, and how to fix it.
All exceptions inherit from SignalFXError.
"""

from __future__ import annotations


class SignalFXError(Exception):
    """Base exception for all SignalFX errors."""

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


class ConfigError(SignalFXError):
    """Base for configuration-related errors."""


class ConfigNotFoundError(ConfigError):
    """~/.signalfx/config.yaml is missing."""


class ConfigValidationError(ConfigError):
    """Config schema or value violation."""


# --- Collector errors ---


class CollectorError(SignalFXError):
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


class AIError(SignalFXError):
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


class RenderError(SignalFXError):
    """Terminal or file rendering failure (typically non-fatal)."""
