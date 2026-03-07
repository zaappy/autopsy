"""Diagnosis orchestrator — wires collectors, AI engine, and renderers.

The orchestrator owns the full pipeline: validate config, collect data
from all registered collectors, pass to the AI engine, and return
the structured result. Rendering is the caller's responsibility.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from autopsy.ai.engine import AIEngine
from autopsy.collectors.cloudwatch import CloudWatchCollector
from autopsy.collectors.github import GitHubCollector
from autopsy.config import AutopsyConfig  # noqa: TC001 — used at runtime for __init__

if TYPE_CHECKING:
    from autopsy.ai.models import DiagnosisResult
    from autopsy.collectors.base import CollectedData

_AI_KEY_ENV_BY_PROVIDER: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


class DiagnosisOrchestrator:
    """Executes the full diagnosis pipeline."""

    def __init__(self, config: AutopsyConfig) -> None:
        """Initialize the orchestrator with a validated config.

        Args:
            config: Validated AutopsyConfig from config.py.
        """
        self.config = config

    def run(
        self,
        *,
        time_window: int | None = None,
        log_groups: list[str] | None = None,
        provider: str | None = None,
    ) -> DiagnosisResult:
        """Execute the full diagnosis pipeline.

        Steps:
        1. Build effective config (apply overrides)
        2. Validate collector and AI credentials
        3. Collect data from CloudWatch and GitHub
        4. Call AI engine and return DiagnosisResult

        Args:
            time_window: Override config time window (minutes).
            log_groups: Override config log groups (replaces list).
            provider: Override AI provider ('anthropic' | 'openai').

        Returns:
            Structured DiagnosisResult from the AI engine.

        Raises:
            ConfigError: On invalid config or missing credentials.
            CollectorError: On data collection failure.
            AIError: On AI provider failure.
        """
        # Effective config: start from loaded config, apply overrides
        aws_dict = self.config.aws.model_dump()
        if time_window is not None:
            aws_dict["time_window"] = time_window
        if log_groups is not None:
            aws_dict["log_groups"] = list(log_groups)

        ai_provider = provider if provider is not None else self.config.ai.provider
        api_key_env = _AI_KEY_ENV_BY_PROVIDER.get(
            ai_provider, self.config.ai.api_key_env
        )
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            from autopsy.utils.errors import AIAuthError

            raise AIAuthError(
                message=f"AI API key env var '{api_key_env}' is not set.",
                hint=f"Export {api_key_env} before running diagnose.",
            )

        # Validate collectors
        cw = CloudWatchCollector()
        gh = GitHubCollector()
        cw.validate_config(aws_dict)
        gh.validate_config(self.config.github.model_dump())

        # Collect
        collected: list[CollectedData] = []
        collected.append(cw.collect(aws_dict))
        collected.append(gh.collect(self.config.github.model_dump()))

        # AI engine
        model = (
            self.config.ai.model
            if ai_provider == self.config.ai.provider
            else ("gpt-4o" if ai_provider == "openai" else self.config.ai.model)
        )
        engine = AIEngine(
            provider=ai_provider,
            model=model,
            api_key=api_key,
            max_tokens=self.config.ai.max_tokens,
            temperature=self.config.ai.temperature,
        )
        return engine.diagnose(collected)
