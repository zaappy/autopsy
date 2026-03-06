"""Rich terminal renderer for diagnosis output.

Produces Slack-pasteable panels: Root Cause, Correlated Deploy,
Suggested Fix, and Timeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autopsy.ai.models import DiagnosisResult


class BaseRenderer(ABC):
    """Abstract renderer interface."""

    @abstractmethod
    def render(self, result: DiagnosisResult) -> None:
        """Output the diagnosis result.

        Args:
            result: Structured diagnosis from the AI engine.
        """


class TerminalRenderer(BaseRenderer):
    """Renders DiagnosisResult as Rich panels to stdout."""

    def render(self, result: DiagnosisResult) -> None:
        """Render diagnosis as colored Rich panels.

        Panels: Root Cause (with confidence bar), Correlated Deploy,
        Suggested Fix, Timeline table. Colors keyed to confidence level.

        Args:
            result: Structured diagnosis from the AI engine.
        """
        raise NotImplementedError
