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
    readable prompt section the LLM can reason over.

    Args:
        collected_data: List of CollectedData from all collectors.

    Returns:
        Formatted user prompt string with logs and deploy context.
    """
    sections: list[str] = []

    logs_data = [cd for cd in collected_data if cd.data_type == "logs"]
    deploy_data = [cd for cd in collected_data if cd.data_type == "deploys"]

    if logs_data:
        sections.append(_format_logs_section(logs_data))

    if deploy_data:
        sections.append(_format_deploys_section(deploy_data))

    other = [cd for cd in collected_data if cd.data_type not in ("logs", "deploys")]
    for cd in other:
        sections.append(
            f"=== {cd.source} ({cd.data_type}) ===\n"
            f"Entries: {cd.entry_count}"
            f"{' (truncated)' if cd.truncated else ''}\n"
            f"{_format_entries(cd.entries)}"
        )

    if not sections:
        return "No data was collected. Unable to perform diagnosis."

    return "\n\n".join(sections)


def _format_logs_section(logs_data: list[CollectedData]) -> str:
    """Format log collector outputs into a prompt section.

    Args:
        logs_data: CollectedData items with data_type='logs'.

    Returns:
        Formatted log section string.
    """
    lines: list[str] = ["=== ERROR LOGS ==="]
    for cd in logs_data:
        start, end = cd.time_range
        lines.append(
            f"Source: {cd.source} | "
            f"Window: {start.isoformat()} → {end.isoformat()} | "
            f"Entries: {cd.entry_count}"
            f"{' (truncated)' if cd.truncated else ''}"
        )
        if cd.raw_query:
            lines.append(f"Query: {cd.raw_query}")
        lines.append("")
        for entry in cd.entries:
            ts = entry.get("@timestamp", entry.get("timestamp", ""))
            msg = entry.get("@message", entry.get("message", ""))
            occ = entry.get("occurrences", 1)
            prefix = f"[{ts}] " if ts else ""
            suffix = f" (×{occ})" if occ > 1 else ""
            lines.append(f"{prefix}{msg}{suffix}")
    return "\n".join(lines)


def _format_deploys_section(deploy_data: list[CollectedData]) -> str:
    """Format deploy collector outputs into a prompt section.

    Args:
        deploy_data: CollectedData items with data_type='deploys'.

    Returns:
        Formatted deploy section string.
    """
    lines: list[str] = ["=== RECENT DEPLOYS ==="]
    for cd in deploy_data:
        start, end = cd.time_range
        source_label = cd.source.capitalize()
        lines.append(
            f"Source: [{source_label}] | "
            f"Window: {start.isoformat()} → {end.isoformat()} | "
            f"Commits: {cd.entry_count}"
        )
        lines.append("")
        for entry in cd.entries:
            sha = entry.get("sha", "unknown")[:8]
            author = entry.get("author", "unknown")
            ts = entry.get("timestamp", "")
            msg = entry.get("message", "").split("\n")[0]
            lines.append(f"  [{sha}] {author} — {msg}")
            if ts:
                lines.append(f"    Timestamp: {ts}")

            pr = entry.get("pr")
            if pr:
                lines.append(f"    PR #{pr.get('number', '?')}: {pr.get('title', '')}")

            file_diffs = entry.get("file_diffs", [])
            if file_diffs:
                lines.append(f"    Changed files ({len(file_diffs)}):")
                for fd in file_diffs:
                    lines.append(
                        f"      {fd['filename']} "
                        f"(+{fd.get('additions', 0)}/-{fd.get('deletions', 0)})"
                    )
                    diff = fd.get("diff", "")
                    if diff:
                        for dline in diff.split("\n")[:20]:
                            lines.append(f"        {dline}")
                        if diff.count("\n") > 20:
                            lines.append("        ...")

            summary = entry.get("files_summary")
            if summary:
                lines.append(f"    {summary}")
            lines.append("")
    return "\n".join(lines)


def _format_entries(entries: list[dict]) -> str:
    """Generic entry formatter as indented JSON.

    Args:
        entries: List of entry dicts.

    Returns:
        JSON-formatted string.
    """
    return json.dumps(entries, indent=2, default=str)
