"""Shared pytest fixtures for SignalFX tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from signalfx.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SuggestedFix,
    TimelineEvent,
)
from signalfx.collectors.base import CollectedData


@pytest.fixture()
def sample_collected_logs() -> CollectedData:
    """Minimal CloudWatch-style collected data for testing."""
    now = datetime.now(tz=timezone.utc)
    return CollectedData(
        source="cloudwatch",
        data_type="logs",
        entries=[
            {"timestamp": now.isoformat(), "message": "ERROR: NullPointerException in handler"},
        ],
        time_range=(now, now),
        raw_query="fields @timestamp, @message | filter @message like /error/i",
        entry_count=1,
        truncated=False,
    )


@pytest.fixture()
def sample_collected_deploys() -> CollectedData:
    """Minimal GitHub-style collected data for testing."""
    now = datetime.now(tz=timezone.utc)
    return CollectedData(
        source="github",
        data_type="deploys",
        entries=[
            {
                "sha": "abc1234",
                "author": "dev@example.com",
                "message": "fix: update handler null check",
                "files": ["src/handler.py"],
            },
        ],
        time_range=(now, now),
        raw_query="last 5 commits on main",
        entry_count=1,
        truncated=False,
    )


@pytest.fixture()
def sample_diagnosis_result() -> DiagnosisResult:
    """Minimal valid DiagnosisResult for renderer/output tests."""
    return DiagnosisResult(
        root_cause=RootCause(
            summary="NullPointerException caused by missing null check in handler",
            category="code_change",
            confidence=0.85,
            evidence=["ERROR: NullPointerException in handler"],
        ),
        correlated_deploy=CorrelatedDeploy(
            commit_sha="abc1234",
            author="dev@example.com",
            pr_title="fix: update handler null check",
            changed_files=["src/handler.py"],
        ),
        suggested_fix=SuggestedFix(
            immediate="Revert commit abc1234 or add null guard in handler.py:42",
            long_term="Add input validation at API boundary; add unit test for null case",
        ),
        timeline=[
            TimelineEvent(time="2026-03-06T10:00:00Z", event="Commit abc1234 merged to main"),
            TimelineEvent(time="2026-03-06T10:05:00Z", event="Deploy completed"),
            TimelineEvent(time="2026-03-06T10:07:00Z", event="First NullPointerException logged"),
        ],
    )
