"""AWS CloudWatch Logs Insights collector.

Pulls error-level logs using the engineer's local AWS credentials.
Implements a multi-stage reduction pipeline: query filter, deduplication,
truncation, and token budgeting.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console

from autopsy.collectors.base import BaseCollector, CollectedData
from autopsy.utils.errors import AWSAuthError, AWSPermissionError, NoDataError

console = Console(stderr=True)

DEFAULT_QUERY = """\
fields @timestamp, @message, @logStream
| filter @message like /(?i)(error|exception|fatal|panic|timeout|5\\d{2})/
| sort @timestamp desc
| limit 200
"""

TOKEN_BUDGET = 6000
MAX_ENTRY_CHARS = 500
MAX_STACK_FRAMES = 5
QUERY_POLL_INTERVAL = 1.0
QUERY_POLL_MAX_WAIT = 120.0
CHARS_PER_TOKEN = 4  # conservative estimate in lieu of tiktoken dependency


class CloudWatchCollector(BaseCollector):
    """Collects error logs from AWS CloudWatch Logs Insights."""

    @property
    def name(self) -> str:
        """Collector identifier."""
        return "cloudwatch"

    def validate_config(self, config: dict) -> bool:
        """Verify AWS credentials and CloudWatch permissions.

        Args:
            config: The 'aws' section of AutopsyConfig.

        Returns:
            True if credentials and permissions are valid.

        Raises:
            AWSAuthError: On expired or invalid credentials.
            AWSPermissionError: On missing logs:StartQuery permission.
        """
        client = _make_client(config)
        try:
            client.describe_log_groups(limit=1)
        except ClientError as exc:
            _raise_for_client_error(exc)
        except BotoCoreError as exc:
            raise AWSAuthError(
                message=f"AWS credential error: {exc}",
                hint="Run 'aws configure' or check AWS_PROFILE.",
                docs_url="https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html",
            ) from exc
        return True

    def collect(self, config: dict) -> CollectedData:
        """Pull error logs from CloudWatch and apply reduction pipeline.

        Args:
            config: The 'aws' section of AutopsyConfig.

        Returns:
            Normalized CollectedData with deduplicated log entries.

        Raises:
            AWSAuthError: On credential failure.
            AWSPermissionError: On IAM permission issues.
            NoDataError: On zero results.
        """
        client = _make_client(config)
        time_window = config.get("time_window", 30)
        log_groups = config.get("log_groups", [])

        end_time = datetime.now(tz=timezone.utc)
        start_time = end_time - timedelta(minutes=time_window)

        raw_entries: list[dict] = []
        for lg in log_groups:
            raw_entries.extend(
                _run_insights_query(client, lg, start_time, end_time)
            )

        total_before = len(raw_entries)

        if total_before == 0:
            raise NoDataError(
                message=f"No error logs found in the last {time_window} minutes.",
                hint=(
                    "Check that your log groups are correct and contain recent "
                    "error-level messages. Try increasing --time-window."
                ),
            )

        # --- Stage 2: Deduplication ---
        entries = _deduplicate(raw_entries)

        # --- Stage 3: Truncation ---
        entries = [_truncate_entry(e) for e in entries]

        # --- Stage 4: Token budget ---
        entries, was_truncated = _apply_token_budget(entries)

        return CollectedData(
            source="cloudwatch",
            data_type="logs",
            entries=entries,
            time_range=(start_time, end_time),
            raw_query=DEFAULT_QUERY.strip(),
            entry_count=total_before,
            truncated=was_truncated or len(entries) < total_before,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_client(config: dict) -> boto3.client:
    """Build a CloudWatch Logs boto3 client from config.

    Args:
        config: The 'aws' section dict.

    Returns:
        boto3 CloudWatch Logs client.
    """
    session_kwargs: dict = {}
    if config.get("profile"):
        session_kwargs["profile_name"] = config["profile"]

    session = boto3.Session(**session_kwargs)
    return session.client("logs", region_name=config.get("region", "us-east-1"))


def _run_insights_query(
    client: boto3.client,
    log_group: str,
    start: datetime,
    end: datetime,
) -> list[dict]:
    """Execute a CloudWatch Logs Insights query and poll for results.

    Args:
        client: boto3 logs client.
        log_group: CloudWatch log group name.
        start: Query start time (UTC).
        end: Query end time (UTC).

    Returns:
        List of raw result dicts with @timestamp, @message, @logStream.

    Raises:
        AWSAuthError: On credential failure.
        AWSPermissionError: On missing IAM permissions.
    """
    try:
        response = client.start_query(
            logGroupName=log_group,
            startTime=int(start.timestamp()),
            endTime=int(end.timestamp()),
            queryString=DEFAULT_QUERY,
        )
    except ClientError as exc:
        _raise_for_client_error(exc)

    query_id = response["queryId"]
    return _poll_query(client, query_id)


def _poll_query(client: boto3.client, query_id: str) -> list[dict]:
    """Poll a Logs Insights query until complete.

    Args:
        client: boto3 logs client.
        query_id: ID from start_query response.

    Returns:
        Flattened list of result entry dicts.
    """
    elapsed = 0.0
    while elapsed < QUERY_POLL_MAX_WAIT:
        result = client.get_query_results(queryId=query_id)
        status = result.get("status", "")
        if status == "Complete":
            return _flatten_results(result.get("results", []))
        if status in ("Failed", "Cancelled", "Timeout"):
            return []
        time.sleep(QUERY_POLL_INTERVAL)
        elapsed += QUERY_POLL_INTERVAL
    return []


def _flatten_results(raw_results: list[list[dict]]) -> list[dict]:
    """Convert Logs Insights result rows into flat dicts.

    Each row is a list of {field, value} dicts. Flatten to {field: value}.

    Args:
        raw_results: Raw results from get_query_results.

    Returns:
        List of flat entry dicts.
    """
    entries = []
    for row in raw_results:
        entry: dict = {}
        for field_obj in row:
            key = field_obj.get("field", "")
            val = field_obj.get("value", "")
            if key and not key.startswith("@ptr"):
                entry[key] = val
        if entry:
            entries.append(entry)
    return entries


def _raise_for_client_error(exc: ClientError) -> None:
    """Map a boto3 ClientError to the appropriate Autopsy exception.

    Args:
        exc: The caught ClientError.

    Raises:
        AWSAuthError: On auth-related error codes.
        AWSPermissionError: On access-denied error codes.
    """
    code = exc.response.get("Error", {}).get("Code", "")

    if code in (
        "ExpiredTokenException",
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "AuthFailure",
    ):
        raise AWSAuthError(
            message=f"AWS authentication failed: {code}",
            hint="Run 'aws configure' or set AWS_PROFILE and try again.",
            docs_url="https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html",
        ) from exc

    if code in ("AccessDeniedException", "AccessDenied"):
        raise AWSPermissionError(
            message=f"AWS permission denied: {code}",
            hint=(
                "Ensure your IAM user/role has logs:StartQuery, "
                "logs:GetQueryResults, and logs:DescribeLogGroups permissions."
            ),
            docs_url="https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/permissions-reference-cwl.html",
        ) from exc

    raise AWSAuthError(
        message=f"AWS API error ({code}): {exc}",
        hint="Check your AWS credentials and region configuration.",
    ) from exc


# ---------------------------------------------------------------------------
# Reduction pipeline
# ---------------------------------------------------------------------------


_TEMPLATE_RE = re.compile(
    r"(0x[0-9a-fA-F]+|"          # hex addresses
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*|"  # timestamps
    r"\b[0-9a-f]{8,}\b|"         # long hex ids
    r"\d+)"                       # any numeric sequence
)


def _template_hash(message: str) -> str:
    """Produce a hash of the message with variable parts normalized.

    Replaces numbers, hex addresses, timestamps, and UUIDs with
    placeholders so structurally identical messages map to the same hash.

    Args:
        message: Raw log message string.

    Returns:
        Hex digest of the normalized template.
    """
    normalized = _TEMPLATE_RE.sub("<*>", message)
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _deduplicate(entries: list[dict]) -> list[dict]:
    """Stage 2: Deduplicate by message template, keeping 1 instance + count.

    Args:
        entries: Raw log entries from the query.

    Returns:
        Deduplicated entries with an 'occurrences' count field.
    """
    seen: dict[str, dict] = {}
    counts: dict[str, int] = {}

    for entry in entries:
        msg = entry.get("@message", "")
        tmpl = _template_hash(msg)
        if tmpl not in seen:
            seen[tmpl] = entry
            counts[tmpl] = 1
        else:
            counts[tmpl] += 1

    result = []
    for tmpl, entry in seen.items():
        entry_copy = dict(entry)
        entry_copy["occurrences"] = counts[tmpl]
        result.append(entry_copy)
    return result


def _truncate_stack_trace(message: str) -> str:
    """Trim stack traces to the top N frames.

    Args:
        message: Log message potentially containing a stack trace.

    Returns:
        Truncated message with at most MAX_STACK_FRAMES stack frames.
    """
    lines = message.split("\n")
    frame_indices: list[int] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("at ", "File ", "Traceback", "  File ")):
            frame_indices.append(i)

    if len(frame_indices) <= MAX_STACK_FRAMES:
        return message

    keep_up_to = frame_indices[MAX_STACK_FRAMES - 1]
    kept = lines[: keep_up_to + 1]
    omitted = len(frame_indices) - MAX_STACK_FRAMES
    kept.append(f"  ... ({omitted} more frames omitted)")
    return "\n".join(kept)


def _truncate_entry(entry: dict) -> dict:
    """Stage 3: Truncate a single entry's message to MAX_ENTRY_CHARS.

    Also trims stack traces to top MAX_STACK_FRAMES frames.

    Args:
        entry: A log entry dict with '@message' key.

    Returns:
        Entry with truncated message.
    """
    msg = entry.get("@message", "")
    msg = _truncate_stack_trace(msg)
    if len(msg) > MAX_ENTRY_CHARS:
        msg = msg[:MAX_ENTRY_CHARS] + "…"
    result = dict(entry)
    result["@message"] = msg
    return result


def _estimate_tokens(entries: list[dict]) -> int:
    """Estimate token count for a list of entries.

    Uses a simple character-based heuristic (1 token ~ 4 chars).

    Args:
        entries: Log entries to measure.

    Returns:
        Estimated token count.
    """
    total_chars = sum(len(str(e)) for e in entries)
    return total_chars // CHARS_PER_TOKEN


def _apply_token_budget(entries: list[dict]) -> tuple[list[dict], bool]:
    """Stage 4: Evict oldest entries (FIFO) until under TOKEN_BUDGET.

    Args:
        entries: Deduplicated and truncated log entries.

    Returns:
        (entries within budget, whether any were evicted).
    """
    if _estimate_tokens(entries) <= TOKEN_BUDGET:
        return entries, False

    kept: list[dict] = []
    for entry in entries:
        candidate = [*kept, entry]
        if _estimate_tokens(candidate) > TOKEN_BUDGET:
            break
        kept.append(entry)

    return kept, True
