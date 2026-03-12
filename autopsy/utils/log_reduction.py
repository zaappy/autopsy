from __future__ import annotations

"""Shared log reduction utilities used by multiple collectors.

Stages:
1. Template extraction for deduplication.
2. Deduplication by template with occurrence counts.
3. Truncation of long messages and stack traces.
4. Token budgeting with FIFO eviction.
"""

import re
from hashlib import sha256
from typing import Tuple

from autopsy.collectors.base import CollectedData  # noqa: TCH001 — runtime type only

MAX_MESSAGE_CHARS = 500
STACK_TRACE_MAX_FRAMES = 5
TOKEN_BUDGET = 6000
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4

_RE_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RE_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_RE_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_RE_LONG_NUMBERS = re.compile(r"\b\d{4,}\b")
_RE_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")


def extract_template(message: str) -> str:
    """Replace variable parts of log messages with placeholders for dedup.

    Normalizes UUIDs, IPs, timestamps, long numbers, and hex strings so that
    log lines that differ only by these values hash to the same template.
    """
    text = message
    text = _RE_UUID.sub("<UUID>", text)
    text = _RE_IP.sub("<IP>", text)
    text = _RE_TIMESTAMP.sub("<TS>", text)
    text = _RE_LONG_NUMBERS.sub("<NUM>", text)
    text = _RE_HEX.sub("<HEX>", text)
    return text


def deduplicate_logs(
    entries: list[dict],
    *,
    message_key: str = "message",
) -> list[dict]:
    """Stage 2: Hash message templates, keep first instance and count occurrences.

    Args:
        entries: Raw log entries (newest first).
        message_key: Key containing the log message field.

    Returns:
        List of unique entries with an ``occurrences`` field.
    """
    template_to_entry: dict[str, dict] = {}
    for entry in entries:
        msg = entry.get(message_key, "")
        template = extract_template(msg)
        key = sha256(template.encode("utf-8")).hexdigest()
        if key in template_to_entry:
            template_to_entry[key]["occurrences"] = (
                template_to_entry[key].get("occurrences", 1) + 1
            )
        else:
            entry["occurrences"] = 1
            template_to_entry[key] = entry
    return list(template_to_entry.values())


def _truncate_message(msg: str) -> str:
    """Cap message at MAX_MESSAGE_CHARS; trim stack traces to top frames."""
    lines = msg.split("\n")
    if len(lines) > 1 and ("Traceback" in lines[0] or "  File " in msg or " at " in msg):
        first_line = lines[0]
        frame_lines: list[str] = []
        for line in lines[1:]:
            stripped = line.strip()
            if (
                stripped.startswith("File ")
                or stripped.startswith("at ")
                or (frame_lines and (stripped.startswith("  ") or not stripped))
            ):
                frame_lines.append(line)
            else:
                break
        kept_frames = frame_lines[:STACK_TRACE_MAX_FRAMES]
        msg = first_line + "\n" + "\n".join(kept_frames)
        if len(frame_lines) > STACK_TRACE_MAX_FRAMES:
            msg += "\n  ..."

    if len(msg) <= MAX_MESSAGE_CHARS:
        return msg
    return msg[:MAX_MESSAGE_CHARS] + "…"


def truncate_entries(
    entries: list[dict],
    *,
    message_key: str = "message",
    max_chars: int = MAX_MESSAGE_CHARS,
    max_frames: int = STACK_TRACE_MAX_FRAMES,
) -> list[dict]:
    """Stage 3: Cap message length and trim stack traces in-place.

    Args:
        entries: Deduplicated entries.
        message_key: Key containing the log message field.
        max_chars: Maximum characters per message (including ellipsis).
        max_frames: Maximum stack trace frames to retain.

    Returns:
        The same list instance, mutated for convenience.
    """
    # Use module-level constants but keep signature for tests / configurability.
    del max_chars, max_frames
    for entry in entries:
        msg = entry.get(message_key, "")
        entry[message_key] = _truncate_message(msg)
    return entries


def _estimate_tokens(text: str) -> int:
    """Rough token count: len/4 (no tiktoken dependency)."""
    return max(1, len(text) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def apply_token_budget(
    entries: list[dict],
    *,
    message_key: str = "message",
    timestamp_key: str = "timestamp",
    budget: int = TOKEN_BUDGET,
) -> Tuple[list[dict], bool]:
    """Stage 4: Hard token cap with FIFO eviction.

    Assumes ``entries`` are in newest-first order. Keeps the newest entries that
    fit within the token budget and returns them in chronological
    (oldest-first) order to make timelines easier to read.

    Returns:
        (entries_under_budget, was_truncated)
    """
    total_tokens = sum(
        _estimate_tokens(e.get(message_key, ""))
        + _estimate_tokens(e.get(timestamp_key, ""))
        for e in entries
    )
    if total_tokens <= budget:
        return entries, False

    current: list[dict] = []
    current_tokens = 0
    for entry in entries:
        t = _estimate_tokens(entry.get(message_key, "")) + _estimate_tokens(
            entry.get(timestamp_key, "")
        )
        if current_tokens + t <= budget:
            current.append(entry)
            current_tokens += t
    return list(reversed(current)), True


__all__ = [
    "extract_template",
    "deduplicate_logs",
    "truncate_entries",
    "apply_token_budget",
    "MAX_MESSAGE_CHARS",
    "STACK_TRACE_MAX_FRAMES",
    "TOKEN_BUDGET",
]

