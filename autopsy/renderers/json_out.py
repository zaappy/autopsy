"""JSON output renderer for --json flag.

Outputs DiagnosisResult as formatted JSON to stdout.
"""

from __future__ import annotations

import json
import sys

from autopsy.ai.models import DiagnosisResult  # noqa: TC001 — used at runtime for render()
from autopsy.renderers.terminal import BaseRenderer


class JSONRenderer(BaseRenderer):
    """Renders DiagnosisResult as JSON to stdout."""

    def _confidence_level(self, confidence: float) -> str:
        """Return Rich style for confidence level (low < 0.5, medium 0.5–0.75, high > 0.75)."""
        if confidence < 0.5:
            return "low"
        if confidence <= 0.75:
            return "medium"
        return "high"

    def render(self, result: DiagnosisResult) -> None:
        """Output diagnosis as indented JSON to stdout.

        Args:
            result: Structured diagnosis from the AI engine.
        """
        rc = result.root_cause
        confidence = self._confidence_level(rc.confidence) if rc else "unknown"
        data = result.model_dump()
        data["root_cause"]["confidence_level"] = confidence
        sys.stdout.write(json.dumps(data, indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
