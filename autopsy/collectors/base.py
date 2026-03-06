"""Abstract collector interface and shared data models.

All data collectors implement BaseCollector. This is the extension point for
adding Datadog, GitLab, and other integrations without touching the core pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime  # noqa: TCH003 — required at runtime by Pydantic

from pydantic import BaseModel, Field


class CollectedData(BaseModel):
    """Normalized output from any collector."""

    source: str = Field(description="Collector name, e.g. 'cloudwatch', 'github'")
    data_type: str = Field(description="'logs' | 'deploys' | 'metrics'")
    entries: list[dict] = Field(default_factory=list, description="Normalized data entries")
    time_range: tuple[datetime, datetime] = Field(
        description="(start_utc, end_utc) of the collected window"
    )
    raw_query: str = Field(default="", description="The query used (for debugging/display)")
    entry_count: int = Field(default=0, description="Total entries before dedup/truncation")
    truncated: bool = Field(default=False, description="Whether data was reduced to fit budget")


class BaseCollector(ABC):
    """Abstract base for all data collectors."""

    @abstractmethod
    def validate_config(self, config: dict) -> bool:
        """Verify credentials and connectivity.

        Args:
            config: Collector-specific config section.

        Returns:
            True if validation passes.

        Raises:
            CollectorError: On auth, permission, or connectivity failure.
        """

    @abstractmethod
    def collect(self, config: dict) -> CollectedData:
        """Pull data and return normalized CollectedData.

        Args:
            config: Collector-specific config section.

        Returns:
            Normalized CollectedData with entries, time range, and metadata.

        Raises:
            CollectorError: On any failure during data collection.
        """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique collector identifier (e.g., 'cloudwatch', 'github')."""
