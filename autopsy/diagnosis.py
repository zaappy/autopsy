"""Diagnosis orchestrator — wires collectors, AI engine, and renderers.

The orchestrator owns the full pipeline: validate config, collect data
from all registered collectors, pass to the AI engine, and return
the structured result. Rendering is the caller's responsibility.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from rich.console import Console

from autopsy.ai.engine import AIEngine
from autopsy.collectors.cloudwatch import CloudWatchCollector
from autopsy.collectors.datadog import DatadogCollector
from autopsy.collectors.github import GitHubCollector
from autopsy.collectors.gitlab import GitLabCollector
from autopsy.config import AutopsyConfig  # noqa: TC001 — used at runtime for __init__

if TYPE_CHECKING:
    from autopsy.ai.models import DiagnosisResult
    from autopsy.collectors.base import BaseCollector, CollectedData

logger = logging.getLogger(__name__)
console = Console(stderr=True)


class _CollectorTask:
    """Pairs a collector with its resolved config dict."""

    __slots__ = ("collector", "config", "role")

    def __init__(
        self, collector: BaseCollector, config: dict, role: str
    ) -> None:
        self.collector = collector
        self.config = config
        self.role = role


class DiagnosisOrchestrator:
    """Executes the full diagnosis pipeline."""

    def __init__(self, config: AutopsyConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Collector discovery
    # ------------------------------------------------------------------

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
        if getattr(self.config, "gitlab", None) is not None:
            gl = GitLabCollector()
            gl._autopsy_role = "gitlab"
            collectors.append(gl)
        return collectors

    # ------------------------------------------------------------------
    # Config resolution per collector
    # ------------------------------------------------------------------

    def _resolve_collector_tasks(
        self,
        aws_dict: dict,
        datadog_dict: dict | None,
    ) -> list[_CollectorTask]:
        """Build the list of collector tasks with resolved configs.

        Collectors whose credentials are missing are skipped with a
        warning — this keeps the skip-logic in one place so both
        sequential and parallel paths share the same behaviour.
        """
        tasks: list[_CollectorTask] = []
        for collector in self._get_collectors():
            role = getattr(collector, "_autopsy_role", "")
            if role == "cloudwatch":
                tasks.append(_CollectorTask(collector, aws_dict, role))
            elif role == "datadog":
                if datadog_dict is None:
                    continue
                api_key_env = datadog_dict.get("api_key_env", "DD_API_KEY")
                app_key_env = datadog_dict.get("app_key_env", "DD_APP_KEY")
                if not os.environ.get(
                    api_key_env, ""
                ).strip() or not os.environ.get(
                    app_key_env, ""
                ).strip():
                    console.print(
                        "[yellow]⚠ Datadog: API or App key not set — "
                        "skipping. CloudWatch + GitHub still "
                        "active.[/yellow]"
                    )
                    continue
                tasks.append(_CollectorTask(collector, datadog_dict, role))
            elif role == "gitlab":
                gitlab_cfg = getattr(self.config, "gitlab", None)
                if gitlab_cfg is None:
                    continue
                token_env = gitlab_cfg.token_env
                if not os.environ.get(token_env, "").strip():
                    console.print(
                        f"[yellow]⚠ GitLab token ({token_env}) not "
                        f"set — skipping GitLab.[/yellow]"
                    )
                    continue
                tasks.append(
                    _CollectorTask(
                        collector, gitlab_cfg.model_dump(), role
                    )
                )
            else:  # github
                tasks.append(
                    _CollectorTask(
                        collector, self.config.github.model_dump(), role
                    )
                )
        return tasks

    # ------------------------------------------------------------------
    # Safe single-collector execution (used by both paths)
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_collect(
        task: _CollectorTask,
    ) -> tuple[CollectedData | None, float, str | None]:
        """Run one collector, returning (data, elapsed_s, error_msg).

        Never raises — collector errors are captured and returned as
        the third element so the caller can decide how to handle them.
        """
        t0 = time.monotonic()
        try:
            task.collector.validate_config(task.config)
            data = task.collector.collect(task.config)
            return data, time.monotonic() - t0, None
        except Exception as exc:
            elapsed = time.monotonic() - t0
            msg = f"{task.role}: {exc}"
            logger.warning("Collector %s failed: %s", task.role, exc)
            return None, elapsed, msg

    # ------------------------------------------------------------------
    # Sequential collection (--sequential / fallback)
    # ------------------------------------------------------------------

    def _collect_sequential(
        self, tasks: list[_CollectorTask]
    ) -> tuple[list[CollectedData], dict[str, float], list[str]]:
        collected: list[CollectedData] = []
        timings: dict[str, float] = {}
        errors: list[str] = []
        for task in tasks:
            data, elapsed, err = self._safe_collect(task)
            timings[task.role] = elapsed
            if err:
                errors.append(err)
            elif data is not None:
                collected.append(data)
        return collected, timings, errors

    # ------------------------------------------------------------------
    # Parallel collection (default)
    # ------------------------------------------------------------------

    async def _async_collect(
        self, tasks: list[_CollectorTask]
    ) -> tuple[list[CollectedData], dict[str, float], list[str]]:
        """Run collectors concurrently via asyncio.to_thread."""

        async def _run(
            task: _CollectorTask,
        ) -> tuple[CollectedData | None, float, str | None, str]:
            data, elapsed, err = await asyncio.to_thread(
                self._safe_collect, task
            )
            return data, elapsed, err, task.role

        results = await asyncio.gather(
            *[_run(t) for t in tasks], return_exceptions=True
        )

        collected: list[CollectedData] = []
        timings: dict[str, float] = {}
        errors: list[str] = []
        for i, res in enumerate(results):
            role = tasks[i].role
            if isinstance(res, BaseException):
                timings[role] = 0.0
                errors.append(f"{role}: {res}")
            else:
                data, elapsed, err, _ = res
                timings[role] = elapsed
                if err:
                    errors.append(err)
                elif data is not None:
                    collected.append(data)
        return collected, timings, errors

    def _collect_parallel(
        self, tasks: list[_CollectorTask]
    ) -> tuple[list[CollectedData], dict[str, float], list[str]]:
        """Run collectors in parallel, handling event-loop edge cases."""
        try:
            asyncio.get_running_loop()
            # Already inside an event loop — fall back to threads.
            return self._collect_threaded(tasks)
        except RuntimeError:
            pass
        return asyncio.run(self._async_collect(tasks))

    def _collect_threaded(
        self, tasks: list[_CollectorTask]
    ) -> tuple[list[CollectedData], dict[str, float], list[str]]:
        """ThreadPoolExecutor fallback when an event loop is running."""
        collected: list[CollectedData] = []
        timings: dict[str, float] = {}
        errors: list[str] = []
        with ThreadPoolExecutor(
            max_workers=len(tasks)
        ) as pool:
            futures = {
                pool.submit(self._safe_collect, t): t for t in tasks
            }
            for future in futures:
                task = futures[future]
                try:
                    data, elapsed, err = future.result()
                except Exception as exc:
                    timings[task.role] = 0.0
                    errors.append(f"{task.role}: {exc}")
                    continue
                timings[task.role] = elapsed
                if err:
                    errors.append(err)
                elif data is not None:
                    collected.append(data)
        return collected, timings, errors

    # ------------------------------------------------------------------
    # Error + progress helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _handle_collector_error(role: str, error: str) -> str:
        """Format a collector error into a consistent warning string."""
        # Strip the "role: " prefix if already present
        detail = error.split(":", 1)[1].strip() if ":" in error else error
        return f"[yellow]✘ {role}: failed — {detail}[/yellow]"

    def _print_collection_summary(
        self,
        tasks: list[_CollectorTask],
        collected: list[CollectedData],
        timings: dict[str, float],
        errors: list[str],
    ) -> None:
        """Print per-collector result lines after collection."""
        error_roles = {
            e.split(":")[0].strip() for e in errors
        }
        for task in tasks:
            elapsed = timings.get(task.role, 0.0)
            if task.role in error_roles:
                # Find the matching error message
                for e in errors:
                    if e.startswith(task.role):
                        console.print(
                            self._handle_collector_error(
                                task.role, e
                            )
                        )
                        break
            else:
                # Find matching collected data for entry count
                count = "ok"
                for cd in collected:
                    if cd.source == task.role:
                        count = (
                            f"{cd.entry_count} entries"
                            if hasattr(cd, "entry_count")
                            else "ok"
                        )
                        break
                console.print(
                    f"[green]✔ {task.role}: "
                    f"{count} ({elapsed:.1f}s)[/green]"
                )

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        time_window: int | None = None,
        log_groups: list[str] | None = None,
        provider: str | None = None,
        sequential: bool = False,
    ) -> DiagnosisResult:
        """Execute the full diagnosis pipeline.

        Args:
            time_window: Override config time window (minutes).
            log_groups: Override config log groups (replaces list).
            provider: Override AI provider ('anthropic' | 'openai').
            sequential: Force sequential collection (for debugging).

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
            datadog_dict.setdefault(
                "time_window", aws_dict.get("time_window", 30)
            )
            if time_window is not None:
                datadog_dict["time_window"] = time_window

        ai_provider = (
            provider if provider is not None else self.config.ai.provider
        )
        api_key = self.config.ai.get_active_api_key(provider=ai_provider)
        if not api_key:
            from autopsy.utils.errors import AIAuthError

            env_name = (
                self.config.ai.anthropic_api_key_env
                if ai_provider == "anthropic"
                else self.config.ai.openai_api_key_env
            )
            provider_name = (
                "Anthropic" if ai_provider == "anthropic" else "OpenAI"
            )
            raise AIAuthError(
                message=f"{provider_name} API key not found.",
                hint=f"Run 'autopsy init' or export {env_name}.",
            )

        # Build collector tasks (skip-logic lives here)
        tasks = self._resolve_collector_tasks(aws_dict, datadog_dict)

        # Collect — parallel by default, sequential on request
        mode = "sequentially" if sequential else "in parallel"
        n = len(tasks)
        with console.status(
            f"[bold]Collecting data from {n} source(s) "
            f"{mode}...[/bold]",
            spinner="dots",
        ):
            if sequential or n <= 1:
                collected, timings, errors = (
                    self._collect_sequential(tasks)
                )
            else:
                collected, timings, errors = (
                    self._collect_parallel(tasks)
                )

        # Per-collector result lines
        self._print_collection_summary(
            tasks, collected, timings, errors
        )

        # Log per-collector timings
        for role, elapsed in timings.items():
            logger.info("Collector %s finished in %.2fs", role, elapsed)

        # Warn on partial failures
        if errors and collected:
            failed_names = ", ".join(
                e.split(":")[0] for e in errors
            )
            console.print(
                f"[yellow]⚠ Partial failure — {failed_names} "
                f"failed. Continuing with {len(collected)} "
                f"source(s).[/yellow]"
            )
        elif errors and not collected:
            from autopsy.utils.errors import CollectorError

            raise CollectorError(
                message="All collectors failed.",
                hint="Check credentials and connectivity. "
                "Errors: " + "; ".join(errors),
            )

        # AI engine
        model = (
            self.config.ai.model
            if ai_provider == self.config.ai.provider
            else (
                "gpt-4o"
                if ai_provider == "openai"
                else self.config.ai.model
            )
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
