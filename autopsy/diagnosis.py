"""Diagnosis orchestrator — wires collectors, AI engine, and renderers.

The orchestrator owns the full pipeline: validate config, collect data
from all registered collectors, pass to the AI engine, and return
the structured result. Rendering is the caller's responsibility.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from rich.console import Console

from autopsy.ai.engine import AIEngine
from autopsy.collectors.cloudwatch import CloudWatchCollector
from autopsy.collectors.datadog import DatadogCollector
from autopsy.collectors.github import GitHubCollector
from autopsy.config import AutopsyConfig  # noqa: TC001 — used at runtime for __init__

if TYPE_CHECKING:
    from autopsy.ai.models import DiagnosisResult
    from autopsy.collectors.base import BaseCollector, CollectedData

console = Console(stderr=True)


class DiagnosisOrchestrator:
    """Executes the full diagnosis pipeline."""

    def __init__(self, config: AutopsyConfig) -> None:
        """Initialize the orchestrator with a validated config.

        Args:
            config: Validated AutopsyConfig from config.py.
        """
        self.config = config

    def _get_collectors(self) -> list[BaseCollector]:
        """Instantiate collectors based on config."""
        collectors: list[BaseCollector] = []
        if self.config.aws:
            cw = CloudWatchCollector()
            cw._autopsy_role = "cloudwatch"
            collectors.append(cw)
        if getattr(self.config, "datadog", None) is not None:
            dd = DatadogCollector()
            dd._autopsy_role = "datadog"
            collectors.append(dd)
        gh = GitHubCollector()
        gh._autopsy_role = "github"
        collectors.append(gh)
        return collectors

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
        start = time.monotonic()
        # Effective config: start from loaded config, apply overrides
        aws_dict = self.config.aws.model_dump()
        if time_window is not None:
            aws_dict["time_window"] = time_window
        if log_groups is not None:
            aws_dict["log_groups"] = list(log_groups)

        datadog_dict: dict | None = None
        if getattr(self.config, "datadog", None) is not None:
            datadog_dict = self.config.datadog.model_dump()
            # If no explicit Datadog time_window override, align with AWS
            datadog_dict.setdefault("time_window", aws_dict.get("time_window", 30))
            if time_window is not None:
                datadog_dict["time_window"] = time_window

        ai_provider = provider if provider is not None else self.config.ai.provider
        api_key = self.config.ai.get_active_api_key(provider=ai_provider)
        if not api_key:
            from autopsy.utils.errors import AIAuthError

            env_name = (
                self.config.ai.anthropic_api_key_env
                if ai_provider == "anthropic"
                else self.config.ai.openai_api_key_env
            )
            provider_name = "Anthropic" if ai_provider == "anthropic" else "OpenAI"
            raise AIAuthError(
                message=f"{provider_name} API key not found.",
                hint=f"Run 'autopsy init' or export {env_name}.",
            )

        # Validate collectors & collect
        collected: list[CollectedData] = []
        for collector in self._get_collectors():
            role = getattr(collector, "_autopsy_role", "")
            if role == "cloudwatch":
                cfg = aws_dict
            elif role == "datadog":
                if datadog_dict is None:
                    continue
                api_key_env = datadog_dict.get("api_key_env", "DD_API_KEY")
                app_key_env = datadog_dict.get("app_key_env", "DD_APP_KEY")
                if not os.environ.get(api_key_env, "").strip() or not os.environ.get(
                    app_key_env, ""
                ).strip():
                    console.print(
                        "[yellow]⚠ Datadog: API or App key not set — skipping. "
                        "CloudWatch + GitHub still active.[/yellow]"
                    )
                    continue
                cfg = datadog_dict
            else:  # github
                cfg = self.config.github.model_dump()
            collector.validate_config(cfg)
            collected.append(collector.collect(cfg))

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
        result = engine.diagnose(collected)
        duration_s = time.monotonic() - start

        # Best-effort history save: never block diagnosis output
        try:
            from autopsy.history import HistoryStore

            with HistoryStore() as store:
                store.save(
                    result=result,
                    duration_s=round(duration_s, 2),
                    log_groups=list(aws_dict.get("log_groups", [])),
                    github_repo=self.config.github.repo,
                    provider=ai_provider,
                    model=model,
                    time_window=int(aws_dict.get("time_window", 0)),
                )
        except Exception:
            pass

        return result
