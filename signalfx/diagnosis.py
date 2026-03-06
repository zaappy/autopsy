"""Diagnosis orchestrator — wires collectors, AI engine, and renderers.

The orchestrator owns the full pipeline: validate config, collect data
from all registered collectors, pass to the AI engine, and return
the structured result. Rendering is the caller's responsibility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from signalfx.ai.models import DiagnosisResult
    from signalfx.config import SignalFXConfig


class DiagnosisOrchestrator:
    """Executes the full diagnosis pipeline."""

    def __init__(self, config: SignalFXConfig) -> None:
        """Initialize the orchestrator with a validated config.

        Args:
            config: Validated SignalFXConfig from config.py.
        """
        self.config = config

    def run(self) -> DiagnosisResult:
        """Execute the full diagnosis pipeline.

        Steps:
        1. Validate config and credentials
        2. Collect data from all registered collectors
        3. Pass collected data to the AI engine
        4. Return DiagnosisResult (rendering is caller's job)

        Returns:
            Structured DiagnosisResult from the AI engine.

        Raises:
            ConfigError: On invalid config or missing credentials.
            CollectorError: On data collection failure.
            AIError: On AI provider failure.
        """
        raise NotImplementedError
