"""JSON output renderer for --json flag.

Outputs DiagnosisResult as formatted JSON to stdout.
"""

from __future__ import annotations

import sys

from autopsy.ai.models import DiagnosisResult  # noqa: TC001 — used at runtime for render()
from autopsy.renderers.terminal import BaseRenderer


class JSONRenderer(BaseRenderer):
    """Renders DiagnosisResult as JSON to stdout."""

    def render(self, result: DiagnosisResult) -> None:
        """Output diagnosis as indented JSON to stdout.

        Args:
            result: Structured diagnosis from the AI engine.
        """
        sys.stdout.write(result.model_dump_json(indent=2))
        sys.stdout.write("\n")
        sys.stdout.flush()
