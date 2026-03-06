"""JSON output renderer for --json flag.

Outputs DiagnosisResult as formatted JSON to stdout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autopsy.renderers.terminal import BaseRenderer

if TYPE_CHECKING:
    from autopsy.ai.models import DiagnosisResult


class JSONRenderer(BaseRenderer):
    """Renders DiagnosisResult as JSON to stdout."""

    def render(self, result: DiagnosisResult) -> None:
        """Output diagnosis as indented JSON to stdout.

        Args:
            result: Structured diagnosis from the AI engine.
        """
        raise NotImplementedError
