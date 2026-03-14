"""Pydantic models for structured AI diagnosis output.

These models define the schema that the LLM must produce. The AI engine
parses raw JSON responses into these models for validation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class RootCause(BaseModel):
    """Root cause analysis from the AI engine."""

    summary: str = Field(description="One-sentence plain-English diagnosis")
    category: str = Field(
        description="code_change | config_change | infra | dependency | traffic | unknown"
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Model self-reported confidence")
    evidence: list[str] = Field(
        default_factory=list, description="Specific log patterns or lines cited"
    )


class CorrelatedDeploy(BaseModel):
    """Deploy most likely correlated with the incident."""

    commit_sha: str | None = None
    author: str | None = None
    pr_title: str | None = None
    changed_files: list[str] = Field(default_factory=list)


class SuggestedFix(BaseModel):
    """Immediate and long-term remediation steps."""

    immediate: str = Field(description="Stop-the-bleeding action")
    long_term: str = Field(description="Prevention for recurrence")


class TimelineEvent(BaseModel):
    """Single event in the incident timeline reconstruction."""

    time: str = Field(description="ISO timestamp")
    event: str = Field(description="What happened at this point")


class SourceInfo(BaseModel):
    """Metadata about a single data source that contributed to the diagnosis."""

    name: str = Field(description="Collector identifier, e.g. 'cloudwatch', 'datadog'")
    data_type: str = Field(description="'logs' | 'deploys' | 'metrics'")
    entry_count: int = Field(description="Number of entries from this source")


class DiagnosisResult(BaseModel):
    """Complete structured output from a diagnosis run."""

    root_cause: RootCause
    correlated_deploy: CorrelatedDeploy
    suggested_fix: SuggestedFix
    timeline: list[TimelineEvent] = Field(default_factory=list)
    sources: list[SourceInfo] = Field(
        default_factory=list,
        description="Data sources that contributed to the diagnosis",
    )
    prompt_version: str = "v1"
    raw_response: str | None = Field(
        default=None, description="Populated on parse-failure fallback"
    )
