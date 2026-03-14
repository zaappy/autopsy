"""Tests for autopsy.renderers.postmortem.PostMortemRenderer."""

from __future__ import annotations

from autopsy.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SourceInfo,
    SuggestedFix,
    TimelineEvent,
)
from autopsy.renderers.postmortem import PostMortemMetadata, PostMortemRenderer


def _minimal_result() -> DiagnosisResult:
    return DiagnosisResult(
        root_cause=RootCause(
            summary="Service crashed due to null pointer",
            category="code_change",
            confidence=0.9,
            evidence=["NullPointerException at UserService.java:42"],
        ),
        correlated_deploy=CorrelatedDeploy(),
        suggested_fix=SuggestedFix(
            immediate="Rollback latest deploy",
            long_term="Add null checks and tests for UserService",
        ),
        timeline=[
            TimelineEvent(time="2026-03-10T10:00:00Z", event="Deploy v123 to production"),
            TimelineEvent(time="2026-03-10T10:05:00Z", event="Error rate spikes"),
        ],
    )


def test_render_returns_valid_markdown(tmp_path) -> None:
    r = PostMortemRenderer()
    result = _minimal_result()
    meta = PostMortemMetadata(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        log_groups=["/aws/lambda/my-func"],
        time_window=30,
        duration_s=42.3,
    )

    md = r.render(result, output_path=None, metadata=meta)

    assert "# Incident Post-Mortem Report" in md
    assert "## Summary" in md
    assert "## Timeline" in md
    assert "## Root Cause Analysis" in md
    assert "## Resolution" in md
    assert "## Action Items" in md
    assert "## Lessons Learned" in md
    assert "## Appendix" in md
    assert "**AI Provider:** anthropic" in md
    assert "**Model:** claude-sonnet-4-20250514" in md
    assert "**Time Window:** 30 minutes" in md
    assert "Diagnosis Duration:" in md
    assert "This report was auto-generated" in md


def test_postmortem_single_source_no_data_sources_table() -> None:
    """One collector: no Data Sources section (matches classic CW+GH)."""
    r = PostMortemRenderer()
    result = _minimal_result()
    result.sources = [SourceInfo(name="cloudwatch", data_type="logs", entry_count=5)]
    md = r.render(result)
    assert "## Data Sources" not in md


def test_postmortem_multi_source_has_data_sources_table() -> None:
    r = PostMortemRenderer()
    result = _minimal_result()
    result.sources = [
        SourceInfo(name="cloudwatch", data_type="logs", entry_count=5),
        SourceInfo(name="datadog", data_type="logs", entry_count=3),
    ]
    md = r.render(result)
    assert "## Data Sources" in md
    assert "| Cloudwatch | Logs | 5 |" in md
    assert "| Datadog | Logs | 3 |" in md


def test_render_minimal_result_without_deploy_or_timeline() -> None:
    r = PostMortemRenderer()
    result = DiagnosisResult(
        root_cause=RootCause(
            summary="Unknown outage in legacy system",
            category="unknown",
            confidence=0.4,
            evidence=[],
        ),
        correlated_deploy=CorrelatedDeploy(),
        suggested_fix=SuggestedFix(
            immediate="Fail over to backup region",
            long_term="Modernize legacy system and add monitoring",
        ),
        timeline=[],
    )

    md = r.render(result)

    # Timeline should include a fallback row.
    assert "| — | No timeline events captured |" in md
    # Correlated deploy section should include the generic message.
    assert "No specific code change was correlated with this incident" in md
    # Evidence fallback message should appear.
    assert "No explicit evidence items captured" in md


def test_generate_filename_is_safe_and_descriptive() -> None:
    r = PostMortemRenderer()
    result = _minimal_result()

    name = r._generate_filename(result)

    assert name.startswith("postmortem-")
    assert name.endswith(".md")
    assert "code_change" in name
    assert "service-crashed-due-to-null-pointer" in name


def test_output_path_writes_file_and_returns_string(tmp_path) -> None:
    r = PostMortemRenderer()
    result = _minimal_result()
    path = tmp_path / "incident.md"

    returned = r.render(result, output_path=path)

    assert returned == str(path)
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "Incident Post-Mortem Report" in content


def test_default_filename_logic_used_by_cli(tmp_path) -> None:
    # This test only asserts that _generate_filename returns a relative path
    # that can be used directly by the CLI.
    r = PostMortemRenderer()
    result = _minimal_result()

    name = r._generate_filename(result)

    p = tmp_path / name
    md = r.render(result)
    p.write_text(md, encoding="utf-8")

    assert p.exists()


def test_markdown_tables_are_well_formed() -> None:
    r = PostMortemRenderer()
    result = _minimal_result()

    md = r.render(result)

    # Basic sanity checks for table header lines.
    assert "| Time (UTC) | Event |" in md
    assert "|------------|-------|" in md
    assert "| # | Action | Owner | Due Date | Status |" in md


def test_special_characters_in_timeline_are_escaped() -> None:
    r = PostMortemRenderer()
    result = DiagnosisResult(
        root_cause=RootCause(
            summary="Pipe character in timeline",
            category="code_change",
            confidence=0.8,
            evidence=["foo | bar"],
        ),
        correlated_deploy=CorrelatedDeploy(),
        suggested_fix=SuggestedFix(
            immediate="Do something",
            long_term="Do something else",
        ),
        timeline=[
            TimelineEvent(time="2026-03-10T10:00:00Z", event="Deploy | weird event"),
        ],
    )

    md = r.render(result)

    # The pipe in the event should be escaped so the table is not broken.
    assert "Deploy \\| weird event" in md


def test_action_items_have_checkboxes() -> None:
    r = PostMortemRenderer()
    result = _minimal_result()

    md = r.render(result)

    assert "- [ ] Rollback latest deploy" in md
    assert "- [ ] Add null checks and tests for UserService" in md

