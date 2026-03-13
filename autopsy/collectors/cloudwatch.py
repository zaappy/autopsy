"""AWS CloudWatch Logs Insights collector.

Pulls error-level logs using the engineer's local AWS credentials.
Implements a multi-stage reduction pipeline: query filter, deduplication,
truncation, and token budgeting.

Stage 1 (query-level filter): Handled by the Logs Insights query regex
filtering for error|exception|fatal|panic|timeout|4xx|5xx — no code needed here.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import boto3
import botocore.exceptions
from rich.console import Console

from autopsy.collectors.base import BaseCollector, CollectedData
from autopsy.utils.errors import (
    AWSAuthError,
    AWSPermissionError,
    CollectorError,
    NoDataError,
)
from autopsy.utils.log_reduction import apply_token_budget, deduplicate_logs, truncate_entries

console = Console(stderr=True)

# Logs Insights query: filter error-level messages, sort newest first, limit 200
INSIGHTS_QUERY = """\
fields @timestamp, @message, @logStream
| filter @message like /(?i)(error|exception|fatal|panic|timeout|[45]\\d{2})/
| sort @timestamp desc
| limit 200
"""

POLL_INTERVALS = (0.5, 1.0, 2.0, 4.0)  # seconds; then 4s until 30s total
POLL_TIMEOUT_TOTAL = 30

MAX_MESSAGE_CHARS = 500
STACK_TRACE_MAX_FRAMES = 5
TOKEN_BUDGET = 6000
TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4

# Patterns for template extraction (dedup)
_RE_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RE_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
_RE_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_RE_LONG_NUMBERS = re.compile(r"\b\d{4,}\b")
_RE_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")


def _extract_template(message: str) -> str:
    """Replace variable parts of log messages with placeholders for dedup.

    Normalizes UUIDs, IPs, timestamps, long numbers, and hex strings so that
    log lines that differ only by these values hash to the same template.

    Args:
        message: Raw log message.

    Returns:
        Normalized template string.
    """
    text = message
    text = _RE_UUID.sub("<UUID>", text)
    text = _RE_IP.sub("<IP>", text)
    text = _RE_TIMESTAMP.sub("<TS>", text)
    text = _RE_LONG_NUMBERS.sub("<NUM>", text)
    text = _RE_HEX.sub("<HEX>", text)
    return text


def _truncate_message(msg: str) -> str:
    """Cap message at MAX_MESSAGE_CHARS; trim stack traces to top 5 frames."""
    lines = msg.split("\n")
    # If it looks like a stack trace, keep first line (exception) + top 5 frames first
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


def _estimate_tokens(text: str) -> int:
    """Rough token count: len/4 (no tiktoken dependency)."""
    return max(1, len(text) // TOKEN_ESTIMATE_CHARS_PER_TOKEN)


def _result_row_to_entry(row: list[dict]) -> dict:
    """Convert a Logs Insights result row (list of field dicts) to our entry format."""
    entry: dict = {}
    for item in row:
        k = item.get("field", "")
        v = item.get("value", "")
        if k == "@timestamp":
            entry["@timestamp"] = v
        elif k == "@message":
            entry["@message"] = v
        elif k == "@logStream":
            entry["@logStream"] = v
        elif k and not k.startswith("@"):
            entry[k] = v
    if "@message" not in entry:
        entry["@message"] = ""
    if "@timestamp" not in entry:
        entry["@timestamp"] = ""
    return entry


class CloudWatchCollector(BaseCollector):
    """Collects error logs from AWS CloudWatch Logs Insights."""

    @property
    def name(self) -> str:
        """Collector identifier."""
        return "cloudwatch"

    def validate_config(self, config: dict) -> bool:
        """Verify AWS credentials and CloudWatch permissions.

        Uses describe_log_groups as a connectivity and auth check, and
        verifies each configured log group exists.

        Args:
            config: The 'aws' section of AutopsyConfig.

        Returns:
            True if credentials and permissions are valid.

        Raises:
            AWSAuthError: On expired or invalid credentials.
            AWSPermissionError: On missing logs:DescribeLogGroups permission.
        """
        region = config.get("region", "us-east-1")
        profile = config.get("profile")
        log_groups = config.get("log_groups", [])

        try:
            session = boto3.Session(region_name=region, profile_name=profile)
            client = session.client("logs")
            for lg in log_groups:
                found = False
                paginator = client.get_paginator("describe_log_groups")
                for page in paginator.paginate(logGroupNamePrefix=lg):
                    for g in page.get("logGroups", []):
                        if g.get("logGroupName") == lg:
                            found = True
                            break
                    if found:
                        break
                if not found:
                    raise AWSPermissionError(
                        message=f"Log group not found: {lg}",
                        hint=f"Check that {lg} exists and you have "
                        "logs:DescribeLogGroups permission.",
                    )
        except botocore.exceptions.NoCredentialsError as exc:
            raise AWSAuthError(
                message="AWS credentials not found",
                hint="Run 'aws configure' or set AWS_PROFILE=<profile> and try again.\n"
                "Docs: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html",
            ) from exc
        except botocore.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("AccessDeniedException", "AccessDenied"):
                raise AWSPermissionError(
                    message="Missing required CloudWatch Logs permissions",
                    hint="IAM role needs: logs:DescribeLogGroups, logs:StartQuery, "
                    "logs:GetQueryResults.\nDocs: https://docs.aws.amazon.com/"
                    "AmazonCloudWatch/latest/logs/iam-identity-based-access-control-cwl.html",
                ) from exc
            if code in ("ExpiredToken", "ExpiredTokenException"):
                raise AWSAuthError(
                    message="AWS credentials have expired",
                    hint="Refresh your credentials: run 'aws sso login' or "
                    "update your session token.",
                ) from exc
            err_msg = exc.response.get("Error", {}).get("Message", str(exc))
            raise CollectorError(
                message=f"AWS CloudWatch error: {err_msg}",
                hint="Check your AWS region and IAM permissions.",
            ) from exc
        return True

    def collect(self, config: dict) -> CollectedData:
        """Pull error logs from CloudWatch and apply reduction pipeline.

        Pipeline stages:
        1. Query-level regex filter (~90% reduction) — in the Insights query.
        2. Deduplication by message template (~60% reduction).
        3. Truncation to 500 chars per entry, stack traces to 5 frames (~40%).
        4. Token budget hard cap at 6000 tokens (FIFO eviction).

        Args:
            config: The 'aws' section of AutopsyConfig.

        Returns:
            Normalized CollectedData with deduplicated log entries.

        Raises:
            AWSAuthError: On credential failure.
            AWSPermissionError: On IAM permission issues.
            CollectorError: On query timeout or failure.
            NoDataError: On zero results.
        """
        region = config.get("region", "us-east-1")
        profile = config.get("profile")
        log_groups = config.get("log_groups", [])
        time_window = int(config.get("time_window", 30))

        end_ts = datetime.now(tz=timezone.utc)
        start_ts = end_ts - timedelta(minutes=time_window)
        start_unix = int(start_ts.timestamp())
        end_unix = int(end_ts.timestamp())

        try:
            session = boto3.Session(region_name=region, profile_name=profile)
            client = session.client("logs")
        except botocore.exceptions.NoCredentialsError as exc:
            raise AWSAuthError(
                message="AWS credentials not found",
                hint="Run 'aws configure' or set AWS_PROFILE=<profile> and try again.\n"
                "Docs: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html",
            ) from exc

        all_rows: list[dict] = []
        for lg in log_groups:
            with console.status(f"Querying CloudWatch log group: {lg}...", spinner="dots"):
                try:
                    query_id = client.start_query(
                        logGroupNames=[lg],
                        startTime=start_unix,
                        endTime=end_unix,
                        queryString=INSIGHTS_QUERY.strip(),
                    ).get("queryId")
                except botocore.exceptions.ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code in ("AccessDeniedException", "AccessDenied"):
                        raise AWSPermissionError(
                            message="Missing required CloudWatch Logs permissions",
                            hint="IAM role needs: logs:DescribeLogGroups, logs:StartQuery, "
                            "logs:GetQueryResults.\nDocs: https://docs.aws.amazon.com/"
                            "AmazonCloudWatch/latest/logs/iam-identity-based-access-control-cwl.html",
                        ) from exc
                    if code in ("ExpiredToken", "ExpiredTokenException"):
                        raise AWSAuthError(
                            message="AWS credentials have expired",
                            hint="Refresh your credentials: run 'aws sso login' or "
                            "update your session token.",
                        ) from exc
                    q_err = exc.response.get("Error", {}).get("Message", str(exc))
                    raise CollectorError(
                        message=f"CloudWatch query failed: {q_err}",
                        hint="Check log group name and IAM permissions.",
                    ) from exc

                if not query_id:
                    raise CollectorError(
                        message="CloudWatch start_query did not return a queryId",
                        hint="Retry or check AWS service status.",
                    )

                # Poll with exponential backoff; max 30s total
                elapsed = 0.0
                for _idx, interval in enumerate(POLL_INTERVALS):
                    if elapsed >= POLL_TIMEOUT_TOTAL:
                        break
                    time.sleep(interval)
                    elapsed += interval
                    resp = client.get_query_results(queryId=query_id)
                    status = resp.get("status", "")
                    if status == "Complete":
                        for row in resp.get("results", []):
                            all_rows.append(_result_row_to_entry(row))
                        break
                    if status == "Failed":
                        raise CollectorError(
                            message="CloudWatch Logs Insights query failed",
                            hint="Check your query and log group; try a smaller time window.",
                        )
                    if status == "Cancelled":
                        raise CollectorError(
                            message="CloudWatch query was cancelled",
                            hint="Retry the diagnosis.",
                        )
                else:
                    # More polls at 4s until timeout
                    while elapsed < POLL_TIMEOUT_TOTAL:
                        time.sleep(4.0)
                        elapsed += 4.0
                        resp = client.get_query_results(queryId=query_id)
                        status = resp.get("status", "")
                        if status == "Complete":
                            for row in resp.get("results", []):
                                all_rows.append(_result_row_to_entry(row))
                            break
                        if status == "Failed":
                            raise CollectorError(
                                message="CloudWatch Logs Insights query failed",
                                hint="Check your query and log group; try a smaller time window.",
                            )
                        if status == "Cancelled":
                            raise CollectorError(
                                message="CloudWatch query was cancelled",
                                hint="Retry the diagnosis.",
                            )
                    else:
                        raise CollectorError(
                            message="CloudWatch query timed out (30s)",
                            hint="Try a smaller time window or fewer log groups.",
                        )

        if not all_rows:
            raise NoDataError(
                message=f"No error-level logs found in the last {time_window} minutes",
                hint="Verify your log_groups in ~/.autopsy/config.yaml point to active log groups.",
            )

        # Stage 2–4: Deduplication, truncation, token budget (shared utilities)
        deduped = deduplicate_logs(all_rows, message_key="@message")
        deduped = truncate_entries(deduped, message_key="@message")
        deduped, truncated = apply_token_budget(
            deduped,
            message_key="@message",
            timestamp_key="@timestamp",
            budget=TOKEN_BUDGET,
        )

        raw_query = (
            f"Logs Insights filter (error|exception|fatal|panic|timeout|4xx|5xx); "
            f"window={time_window}m; groups={len(log_groups)}"
        )
        return CollectedData(
            source="cloudwatch",
            data_type="logs",
            entries=deduped,
            time_range=(start_ts, end_ts),
            raw_query=raw_query,
            entry_count=len(all_rows),
            truncated=truncated,
        )
