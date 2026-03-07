"""AI provider abstraction and response parser.

The orchestrator calls AIEngine.diagnose() — it never touches provider APIs
directly. The engine handles prompt construction, provider dispatch, JSON
parsing, retry logic, and fallback behavior.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pydantic import ValidationError
from rich.console import Console

if TYPE_CHECKING:
    from autopsy.collectors.base import CollectedData

from autopsy.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SuggestedFix,
)
from autopsy.ai.prompts import CORRECTION_PROMPT, SYSTEM_PROMPT_V1, build_user_prompt
from autopsy.utils.errors import (
    AIAuthError,
    AIRateLimitError,
    AIResponseError,
    AITimeoutError,
)

console = Console(stderr=True)

API_TIMEOUT = 60


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


class BaseProvider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
    def chat(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Send a chat completion request and return the raw text response.

        Args:
            system: System prompt.
            messages: List of {"role": ..., "content": ...} dicts.
            model: Model identifier.
            max_tokens: Max response tokens.
            temperature: Sampling temperature.

        Returns:
            Raw text content from the LLM.
        """


class AnthropicProvider(BaseProvider):
    """Claude API via the anthropic SDK."""

    def __init__(self, api_key: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, timeout=API_TIMEOUT)

    def chat(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Call Anthropic Messages API.

        Args:
            system: System prompt.
            messages: Conversation messages.
            model: Anthropic model ID.
            max_tokens: Max response tokens.
            temperature: Sampling temperature.

        Returns:
            Text content from Claude's response.

        Raises:
            AIAuthError: On 401 authentication failure.
            AIRateLimitError: On 429 rate limit.
            AITimeoutError: On request timeout.
        """
        import anthropic

        try:
            response = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=messages,
            )
        except anthropic.AuthenticationError as exc:
            raise AIAuthError(
                message="Anthropic API key is invalid.",
                hint="Check that ANTHROPIC_API_KEY is set correctly.",
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AIRateLimitError(
                message="Anthropic rate limit exceeded.",
                hint="Wait a moment and try again, or check your plan quota.",
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise AITimeoutError(
                message=f"Anthropic API timed out after {API_TIMEOUT}s.",
                hint="The model may be overloaded. Try again shortly.",
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AITimeoutError(
                message=f"Failed to connect to Anthropic API: {exc}",
                hint="Check your network connection.",
            ) from exc

        return response.content[0].text


class OpenAIProvider(BaseProvider):
    """OpenAI Chat Completions API."""

    def __init__(self, api_key: str) -> None:
        import openai

        self._client = openai.OpenAI(api_key=api_key, timeout=API_TIMEOUT)

    def chat(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Call OpenAI Chat Completions API.

        Args:
            system: System prompt.
            messages: Conversation messages.
            model: OpenAI model ID.
            max_tokens: Max response tokens.
            temperature: Sampling temperature.

        Returns:
            Text content from the model's response.

        Raises:
            AIAuthError: On 401 authentication failure.
            AIRateLimitError: On 429 rate limit.
            AITimeoutError: On request timeout.
        """
        import openai

        all_messages = [{"role": "system", "content": system}, *messages]
        try:
            response = self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=all_messages,
            )
        except openai.AuthenticationError as exc:
            raise AIAuthError(
                message="OpenAI API key is invalid.",
                hint="Check that OPENAI_API_KEY is set correctly.",
            ) from exc
        except openai.RateLimitError as exc:
            raise AIRateLimitError(
                message="OpenAI rate limit exceeded.",
                hint="Wait a moment and try again, or check your plan quota.",
            ) from exc
        except openai.APITimeoutError as exc:
            raise AITimeoutError(
                message=f"OpenAI API timed out after {API_TIMEOUT}s.",
                hint="The model may be overloaded. Try again shortly.",
            ) from exc
        except openai.APIConnectionError as exc:
            raise AITimeoutError(
                message=f"Failed to connect to OpenAI API: {exc}",
                hint="Check your network connection.",
            ) from exc

        return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


_PROVIDERS: dict[str, type[BaseProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
}


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

        Raises:
            ValueError: If the provider is not supported.
        """
        self.provider_name = provider
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature

        provider_cls = _PROVIDERS.get(provider)
        if provider_cls is None:
            msg = f"Unsupported AI provider: '{provider}'. Use 'anthropic' or 'openai'."
            raise ValueError(msg)
        self._provider: BaseProvider = provider_cls(api_key)

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
        system_prompt, user_prompt = self._build_prompt(collected_data)

        messages = [{"role": "user", "content": user_prompt}]
        raw = self._provider.chat(
            system_prompt,
            messages,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        # Attempt 1: parse response
        result = _try_parse(raw)
        if result is not None:
            return result

        # Retry: send correction prompt with the original response as context
        console.print("[yellow]Malformed JSON — retrying with correction prompt…[/yellow]")
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": CORRECTION_PROMPT})

        retry_raw = self._provider.chat(
            system_prompt,
            messages,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        result = _try_parse(retry_raw)
        if result is not None:
            return result

        # Fallback: return raw text in a minimal DiagnosisResult
        console.print("[yellow]Retry also failed — falling back to raw response.[/yellow]")
        return _fallback_result(retry_raw)

    def _build_prompt(
        self, collected_data: list[CollectedData]
    ) -> tuple[str, str]:
        """Construct system and user prompts from collected data.

        Args:
            collected_data: Normalized data from all collectors.

        Returns:
            (system_prompt, user_prompt) tuple.
        """
        return SYSTEM_PROMPT_V1, build_user_prompt(collected_data)

    def _parse_response(self, raw: str) -> DiagnosisResult:
        """Parse raw JSON string into a validated DiagnosisResult.

        Args:
            raw: Raw JSON string from the LLM.

        Returns:
            Validated DiagnosisResult.

        Raises:
            AIResponseError: On invalid JSON or schema mismatch.
        """
        result = _try_parse(raw)
        if result is not None:
            return result
        raise AIResponseError(
            message="Failed to parse AI response as valid DiagnosisResult JSON.",
            hint="The model returned an unexpected format. Try again.",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_json(raw: str) -> str:
    """Extract a JSON object from a response that may contain surrounding text.

    Handles common LLM patterns like markdown code fences around JSON.

    Args:
        raw: Raw LLM response text.

    Returns:
        Cleaned string likely to be valid JSON.
    """
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else 3
        text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    return text


def _try_parse(raw: str) -> DiagnosisResult | None:
    """Attempt to parse raw text into a DiagnosisResult.

    Args:
        raw: Raw response text from the LLM.

    Returns:
        DiagnosisResult if parsing succeeds, None otherwise.
    """
    try:
        cleaned = _extract_json(raw)
        data = json.loads(cleaned)
        return DiagnosisResult(**data)
    except (json.JSONDecodeError, ValidationError, TypeError, KeyError):
        return None


def _fallback_result(raw: str) -> DiagnosisResult:
    """Build a minimal DiagnosisResult with the raw response preserved.

    Args:
        raw: The unparseable LLM response.

    Returns:
        DiagnosisResult with raw_response populated.
    """
    return DiagnosisResult(
        root_cause=RootCause(
            summary="Unable to parse structured diagnosis — see raw response.",
            category="unknown",
            confidence=0.0,
            evidence=[],
        ),
        correlated_deploy=CorrelatedDeploy(),
        suggested_fix=SuggestedFix(
            immediate="Review the raw AI response below for actionable insights.",
            long_term="Report this parsing failure so prompts can be improved.",
        ),
        raw_response=raw,
    )
