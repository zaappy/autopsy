"""Versioned prompt templates for the AI diagnostic engine.

Prompts are treated as code: versioned, tested, and evaluated against
a known incident corpus. Each version is a named constant.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autopsy.collectors.base import CollectedData

SYSTEM_PROMPT_V1 = """\
You are an expert Site Reliability Engineer performing incident root cause \
analysis. You will receive:

1. Error logs from a production environment (last N minutes)
2. Recent deployment diffs and metadata

You may receive data from multiple log sources (e.g., CloudWatch AND Datadog) \
and multiple code sources (e.g., GitHub AND GitLab). When multiple sources are \
present, cross-correlate timestamps and events across sources. The root cause \
may only become apparent when data from different systems is combined.

Your task: Identify the most likely root cause of the errors by correlating \
log patterns with recent code changes.

Respond ONLY with valid JSON matching this schema:

{
  "root_cause": {
    "summary": "One-sentence plain-English diagnosis",
    "category": "code_change | config_change | infra | dependency | traffic | unknown",
    "confidence": 0.0-1.0,
    "evidence": ["Specific log lines or patterns cited"]
  },
  "correlated_deploy": {
    "commit_sha": "abc123 or null if no correlation",
    "author": "commit author",
    "pr_title": "PR title if available",
    "changed_files": ["files most likely related"]
  },
  "suggested_fix": {
    "immediate": "What to do right now to stop bleeding",
    "long_term": "What to do to prevent recurrence"
  },
  "timeline": [
    {"time": "ISO timestamp", "event": "description"}
  ]
}
"""

CORRECTION_PROMPT = """\
Your previous response was not valid JSON. Please respond ONLY with valid JSON \
matching the schema provided in the system prompt. Do not include any text before \
or after the JSON object.
"""


def build_user_prompt(collected_data: list[CollectedData]) -> str:
    """Assemble collected logs and deploys into a structured context block.

    Groups entries by data type (logs, deploys) and formats them into a
    readable prompt section the LLM can reason over.  When multiple sources
    contribute to the same data type each source gets its own labeled
    sub-section so the AI can cross-correlate across systems.

    Args:
        collected_data: List of CollectedData from all collectors.

    Returns:
        Formatted user prompt string with logs and deploy context.
    """
    sections: list[str] = []

    log_sources = [cd for cd in collected_data if cd.data_type == "logs"]
    deploy_sources = [cd for cd in collected_data if cd.data_type == "deploys"]

    sections.append(_format_logs_section(log_sources))
    sections.append(_format_deploys_section(deploy_sources))

    other = [cd for cd in collected_data if cd.data_type not in ("logs", "deploys")]
    for cd in other:
        sections.append(
            f"=== {cd.source} ({cd.data_type}) ===\n"
            f"Entries: {cd.entry_count}"
            f"{' (truncated)' if cd.truncated else ''}\n"
            f"{_format_entries(cd.entries)}"
        )

    # Any multi-collector run (e.g. CloudWatch + GitHub): correlate across systems
    if len(collected_data) > 1:
        sections.append(_format_cross_source_hint(collected_data))

    if not any(log_sources or deploy_sources or other):
        return "No data was collected. Unable to perform diagnosis."

    return "\n\n".join(sections)


def _format_logs_section(logs_data: list[CollectedData]) -> str:
    """Format log collector outputs into a prompt section.

    Each source gets a labeled sub-section when multiple sources provide
    logs.  Single-source output is visually identical to the old format.

    Args:
        logs_data: CollectedData items with data_type='logs'.

    Returns:
        Formatted log section string.
    """
    lines: list[str] = ["=== ERROR LOGS ==="]

    if not logs_data:
        lines.append("No log data available. Diagnose based on deploy data only.")
        return "\n".join(lines)

    for cd in logs_data:
        lines.append("")
        lines.append(f"--- Source: {cd.source.title()} ---")
        if cd.raw_query:
            lines.append(f"Query: {cd.raw_query}")
        lines.append(
            f"Entries: {len(cd.entries)} (from {cd.entry_count} raw)"
        )
        if cd.truncated:
            lines.append("\u26a0 Data was truncated to fit token budget")
        lines.append("")
        for entry in cd.entries:
            ts = entry.get("@timestamp", entry.get("timestamp", ""))
            msg = entry.get("@message", entry.get("message", ""))
            level = entry.get("log_level", "ERROR").upper()
            occ = entry.get("occurrences", 1)
            line = f"[{ts}] [{level}] {msg}" if ts else f"[{level}] {msg}"
            if occ > 1:
                line += f" (\u00d7{occ})"
            lines.append(line)

    return "\n".join(lines)


def _format_deploys_section(deploy_data: list[CollectedData]) -> str:
    """Format deploy collector outputs into a prompt section.

    Each source gets a labeled sub-section so the AI knows which VCS
    system each commit came from.

    Args:
        deploy_data: CollectedData items with data_type='deploys'.

    Returns:
        Formatted deploy section string.
    """
    lines: list[str] = ["=== RECENT DEPLOYS ==="]

    if not deploy_data:
        lines.append("No deployment data available. GitHub/GitLab not configured.")
        lines.append("Diagnose based on log patterns only.")
        return "\n".join(lines)

    for cd in deploy_data:
        lines.append("")
        lines.append(f"--- Source: {cd.source.title()} ---")
        if cd.raw_query:
            lines.append(f"Query: {cd.raw_query}")
        lines.append(f"Commits: {len(cd.entries)}")
        lines.append("")
        for entry in cd.entries:
            sha = entry.get("sha", "unknown")[:8]
            author = entry.get("author", "unknown")
            ts = entry.get("timestamp", "")
            msg = entry.get("message", "").split("\n")[0]
            mr_title = entry.get("mr_title") or entry.get("pr_title") or ""

            lines.append(f"[{sha}] {ts} \u2014 \"{msg}\" by @{author}")
            if mr_title:
                lines.append(f"  MR/PR: {mr_title}")

            pr = entry.get("pr")
            if pr:
                lines.append(f"  PR #{pr.get('number', '?')}: {pr.get('title', '')}")

            file_diffs = entry.get("file_diffs", [])
            diffs = entry.get("diffs", file_diffs)
            if diffs:
                files = [
                    d.get("new_path", d.get("filename", "?"))
                    for d in diffs[:5]
                ]
                lines.append(f"  Files: {', '.join(files)}")
                for diff_item in diffs[:3]:
                    diff_text = diff_item.get("diff", diff_item.get("patch", ""))
                    if diff_text:
                        diff_lines = diff_text.split("\n")[:20]
                        lines.append("  ```")
                        lines.extend(f"  {dl}" for dl in diff_lines)
                        lines.append("  ```")
            elif entry.get("files"):
                lines.append(f"  Files: {', '.join(entry['files'][:10])}")

            summary = entry.get("files_summary")
            if summary:
                lines.append(f"  {summary}")
            lines.append("")

    return "\n".join(lines)


def _format_cross_source_hint(collected_data: list[CollectedData]) -> str:
    """Build the cross-source analysis instruction block.

    Included when more than one collector contributed data (logs + deploys,
    or multiple log sources, etc.) so the model correlates across systems.

    Args:
        collected_data: All collected data items.

    Returns:
        Formatted instruction block string.
    """
    lines: list[str] = ["=== CROSS-SOURCE ANALYSIS INSTRUCTIONS ==="]
    lines.append("You are receiving data from MULTIPLE sources.")
    sources = [d.source for d in collected_data]
    lines.append(f"Active sources: {', '.join(sources)}")
    lines.append("IMPORTANT: Correlate timestamps across sources.")
    lines.append(
        "Look for: errors in one system that started after a deploy in another."
    )
    lines.append(
        "The root cause may only be visible by combining data from different sources."
    )
    return "\n".join(lines)


def _format_entries(entries: list[dict]) -> str:
    """Generic entry formatter as indented JSON.

    Args:
        entries: List of entry dicts.

    Returns:
        JSON-formatted string.
    """
    return json.dumps(entries, indent=2, default=str)
