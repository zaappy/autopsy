"""AI provider abstraction and response parser.

The orchestrator calls AIEngine.diagnose() — it never touches provider APIs
directly. The engine handles prompt construction, provider dispatch, JSON
parsing, retry logic, and fallback behavior.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from signalfx.ai.models import DiagnosisResult
    from signalfx.collectors.base import CollectedData


class AIEngine:
    """Unified interface to LLM providers for incident diagnosis."""

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ) -> None:
        """Initialize the AI engine.

        Args:
            provider: 'anthropic' or 'openai'.
            model: Model identifier (e.g., 'claude-sonnet-4-20250514').
            api_key: API key for the selected provider.
            max_tokens: Maximum response tokens.
            temperature: Sampling temperature (low = deterministic).
        """
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature

    def diagnose(self, collected_data: list[CollectedData]) -> DiagnosisResult:
        """Build prompt from collected data, call LLM, parse response.

        Retries once on malformed JSON. Falls back to raw text on second failure.

        Args:
            collected_data: Normalized data from all collectors.

        Returns:
            Structured DiagnosisResult (or raw_response on fallback).

        Raises:
            AIAuthError: On invalid API key.
            AIRateLimitError: On rate limit / quota exceeded.
            AITimeoutError: On response timeout (60s).
        """
        raise NotImplementedError

    def _build_prompt(self, collected_data: list[CollectedData]) -> tuple[str, str]:
        """Construct system and user prompts from collected data.

        Args:
            collected_data: Normalized data from all collectors.

        Returns:
            (system_prompt, user_prompt) tuple.
        """
        raise NotImplementedError

    def _parse_response(self, raw: str) -> DiagnosisResult:
        """Parse raw JSON string into a validated DiagnosisResult.

        Args:
            raw: Raw JSON string from the LLM.

        Returns:
            Validated DiagnosisResult.

        Raises:
            AIResponseError: On invalid JSON or schema mismatch.
        """
        raise NotImplementedError
