"""Versioned prompt templates for the AI diagnostic engine.

Prompts are treated as code: versioned, tested, and evaluated against
a known incident corpus. Each version is a named constant.
"""

from __future__ import annotations

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

    Args:
        collected_data: List of CollectedData from all collectors.

    Returns:
        Formatted user prompt string with logs and deploy context.
    """
    raise NotImplementedError
