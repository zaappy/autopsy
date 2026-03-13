from __future__ import annotations

"""Datadog Logs Search API collector.

Pulls error-level logs from Datadog's Logs Search API, applies the shared
4-stage reduction pipeline, and returns normalized CollectedData.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rich.console import Console

from autopsy.collectors.base import BaseCollector, CollectedData
from autopsy.utils.errors import (
    CollectorError,
    DatadogAuthError,
    DatadogRateLimitError,
    NoDataError,
)
from autopsy.utils.log_reduction import apply_token_budget, deduplicate_logs, truncate_entries

console = Console(stderr=True)

SITE_URLS: dict[str, str] = {
    "us1": "https://api.datadoghq.com",
    "eu1": "https://api.datadoghq.eu",
    "us3": "https://api.us3.datadoghq.com",
    "us5": "https://api.us5.datadoghq.com",
    "ap1": "https://api.ap1.datadoghq.com",
}

LOGS_PAGE_LIMIT = 200
MAX_ENTRIES = 200
REQUEST_TIMEOUT = 30


def _resolve_base_url(site: str) -> str:
    try:
        return SITE_URLS[site]
    except KeyError as exc:
        raise CollectorError(
            message=f"Invalid Datadog site: {site}",
            hint="Valid options: us1, eu1, us3, us5, ap1.",
        ) from exc


def _http_request(
    base_url: str,
    path: str,
    *,
    api_key: str,
    app_key: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{base_url}{path}"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
    }
    data_bytes: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = json.dumps(body).encode("utf-8")

    req = Request(url, data=data_bytes, headers=headers, method=method)

    try:
        resp = urlopen(req, timeout=REQUEST_TIMEOUT)
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
    except HTTPError as exc:
        status = exc.code
        if status in (401, 403):
            raise DatadogAuthError(
                message="Datadog authentication failed",
                hint=(
                    "Check DD_API_KEY and DD_APP_KEY env vars.\n"
                    "Generate keys: app.datadoghq.com → Organization Settings → API Keys"
                ),
                docs_url="https://docs.datadoghq.com/account_management/api-app-keys/",
            ) from exc
        if status == 429:
            raise DatadogRateLimitError(
                message="Datadog API rate limit exceeded",
                hint=(
                    "Wait 60 seconds and retry. Consider narrowing your query with "
                    "service/source filters."
                ),
                docs_url="https://docs.datadoghq.com/api/latest/rate-limits/",
            ) from exc
        if status == 400:
            raise CollectorError(
                message="Invalid Datadog query",
                hint="Check your service and source filters in config.",
            ) from exc
        raise CollectorError(
            message=f"Datadog API error {status}",
            hint="See Datadog API logs for more details.",
        ) from exc
    except URLError as exc:
        raise CollectorError(
            message=f"Cannot connect to Datadog API at {base_url}",
            hint=(
                "Verify your site setting. Available: us1, eu1, us3, us5, ap1."
            ),
        ) from exc


class DatadogCollector(BaseCollector):
    """Collects error-level logs from Datadog Logs Search API."""

    @property
    def name(self) -> str:
        """Collector identifier."""
        return "datadog"

    def validate_config(self, config: dict) -> bool:
        """Verify Datadog API and application keys via /api/v1/validate."""
        api_key_env = config.get("api_key_env", "DD_API_KEY")
        app_key_env = config.get("app_key_env", "DD_APP_KEY")
        site = config.get("site", "us1")

        api_key = os.environ.get(api_key_env, "")
        app_key = os.environ.get(app_key_env, "")
        if not api_key or not app_key:
            raise DatadogAuthError(
                message="Datadog API key or App key not found in environment.",
                hint=(
                    f"Check env vars {api_key_env} and {app_key_env}.\n"
                    "Generate keys: app.datadoghq.com → Organization Settings → API Keys"
                ),
                docs_url="https://docs.datadoghq.com/account_management/api-app-keys/",
            )

        base_url = _resolve_base_url(site)

        _http_request(
            base_url,
            "/api/v1/validate",
            api_key=api_key,
            app_key=app_key,
            method="GET",
        )
        return True

    def collect(self, config: dict) -> CollectedData:
        """Pull error-level logs from Datadog and apply reduction pipeline."""
        api_key_env = config.get("api_key_env", "DD_API_KEY")
        app_key_env = config.get("app_key_env", "DD_APP_KEY")
        site = config.get("site", "us1")
        service = config.get("service")
        source = config.get("source")
        time_window = int(config.get("time_window", 30))

        api_key = os.environ.get(api_key_env, "")
        app_key = os.environ.get(app_key_env, "")
        if not api_key or not app_key:
            raise DatadogAuthError(
                message="Datadog API key or App key not found in environment.",
                hint=(
                    f"Check env vars {api_key_env} and {app_key_env}.\n"
                    "Generate keys: app.datadoghq.com → Organization Settings → API Keys"
                ),
                docs_url="https://docs.datadoghq.com/account_management/api-app-keys/",
            )

        base_url = _resolve_base_url(site)

        end_ts = datetime.now(tz=timezone.utc)
        start_ts = end_ts - timedelta(minutes=time_window)

        query_parts = ["status:(error OR critical)"]
        if service:
            query_parts.append(f"service:{service}")
        if source:
            query_parts.append(f"source:{source}")
        query = " ".join(query_parts)

        entries: list[dict[str, Any]] = []
        page_cursor: str | None = None

        with console.status(
            f"Querying Datadog logs for service: {service or '*'}...", spinner="dots"
        ):
            while True:
                body: dict[str, Any] = {
                    "filter": {
                        "query": query,
                        "from": start_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "to": end_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "indexes": ["*"],
                    },
                    "sort": "-timestamp",
                    "page": {
                        "limit": LOGS_PAGE_LIMIT,
                    },
                }
                if page_cursor:
                    body["page"]["cursor"] = page_cursor

                resp = _http_request(
                    base_url,
                    "/api/v2/logs/events/search",
                    api_key=api_key,
                    app_key=app_key,
                    method="POST",
                    body=body,
                )

                data = resp.get("data") or []
                for item in data:
                    entries.append(self._normalize_entry(item))
                    if len(entries) >= MAX_ENTRIES:
                        break

                if len(entries) >= MAX_ENTRIES:
                    break

                meta = resp.get("meta") or {}
                page_info = meta.get("page") or {}
                cursor = page_info.get("after")
                if not cursor:
                    break
                page_cursor = cursor

        if not entries:
            raise NoDataError(
                message=(
                    f"No error-level logs found in Datadog for the last {time_window} minutes"
                ),
                hint=(
                    "Verify your service/source filters. "
                    "Check that your application is sending logs to Datadog."
                ),
            )

        deduped = deduplicate_logs(entries, message_key="message")
        deduped = truncate_entries(deduped, message_key="message")
        deduped, truncated = apply_token_budget(
            deduped,
            message_key="message",
            timestamp_key="timestamp",
        )

        raw_query = (
            f"Datadog Logs Search: {query}; "
            f"window={time_window}m; site={site}"
        )

        return CollectedData(
            source="datadog",
            data_type="logs",
            entries=deduped,
            time_range=(start_ts, end_ts),
            raw_query=raw_query,
            entry_count=len(entries),
            truncated=truncated,
        )

    def _normalize_entry(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Convert Datadog log format to standard CollectedData entry."""
        attrs = raw.get("attributes", {}) or {}
        return {
            "timestamp": attrs.get("timestamp", ""),
            "message": attrs.get("message", ""),
            "service": attrs.get("service", "unknown"),
            "host": attrs.get("host", ""),
            "source": "datadog",
            "log_level": attrs.get("status", "error"),
            "raw_id": raw.get("id", ""),
        }

