from __future__ import annotations

"""Tests for autopsy.collectors.datadog — Datadog log collector."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from urllib.error import HTTPError, URLError

from autopsy.collectors.base import CollectedData
from autopsy.collectors.datadog import DatadogCollector, SITE_URLS
from autopsy.config import AutopsyConfig, AWSConfig, DatadogConfig, GitHubConfig
from autopsy.utils.errors import (
    CollectorError,
    ConfigValidationError,
    DatadogAuthError,
    DatadogRateLimitError,
    NoDataError,
)


def _dd_config(
    site: str = "us1",
    service: str | None = None,
    source: str | None = None,
    time_window: int = 30,
) -> dict:
    return {
        "api_key_env": "DD_API_KEY",
        "app_key_env": "DD_APP_KEY",
        "site": site,
        "service": service,
        "source": source,
        "time_window": time_window,
    }


def _dd_response(
    entries: List[Dict[str, Any]],
    cursor: str | None = None,
) -> Dict[str, Any]:
    data = []
    for e in entries:
        data.append(
            {
                "id": e.get("id", "log-id"),
                "attributes": {
                    "timestamp": e.get("timestamp", "2026-03-11T02:15:33.445Z"),
                    "message": e.get("message", "error"),
                    "status": e.get("status", "error"),
                    "service": e.get("service", "svc"),
                    "host": e.get("host", "host"),
                },
            }
        )
    meta: Dict[str, Any] = {}
    if cursor is not None:
        meta = {"page": {"after": cursor}}
    return {"data": data, "meta": meta}


def _http_error(status: int) -> HTTPError:
    return HTTPError("http://example", status, "err", hdrs=None, fp=None)


class TestValidateConfig:
    @patch("autopsy.collectors.datadog.urlopen")
    def test_validate_success(self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")
        resp = MagicMock()
        resp.read.return_value = b'{"valid": true}'
        mock_urlopen.return_value = resp

        collector = DatadogCollector()
        assert collector.validate_config(_dd_config()) is True

    @patch("autopsy.collectors.datadog.urlopen")
    def test_validate_bad_api_key_403(self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_API_KEY", "bad")
        monkeypatch.setenv("DD_APP_KEY", "bad")
        mock_urlopen.side_effect = _http_error(403)

        collector = DatadogCollector()
        with pytest.raises(DatadogAuthError) as exc_info:
            collector.validate_config(_dd_config())
        assert "Datadog authentication failed" in exc_info.value.message
        assert "DD_API_KEY" in exc_info.value.hint

    @patch("autopsy.collectors.datadog.urlopen")
    def test_validate_bad_app_key_401(self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "bad")
        mock_urlopen.side_effect = _http_error(401)

        collector = DatadogCollector()
        with pytest.raises(DatadogAuthError):
            collector.validate_config(_dd_config())

    @patch("autopsy.collectors.datadog.urlopen")
    def test_validate_rate_limit_429(self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")
        mock_urlopen.side_effect = _http_error(429)

        collector = DatadogCollector()
        with pytest.raises(DatadogRateLimitError):
            collector.validate_config(_dd_config())

    @patch("autopsy.collectors.datadog.urlopen")
    def test_validate_connection_error_hints_site(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")
        mock_urlopen.side_effect = URLError("connection refused")

        collector = DatadogCollector()
        with pytest.raises(CollectorError) as exc_info:
            collector.validate_config(_dd_config(site="eu1"))
        assert "Cannot connect to Datadog API" in exc_info.value.message
        assert "Available: us1, eu1, us3, us5, ap1." in exc_info.value.hint


class TestCollect:
    @patch("autopsy.collectors.datadog.urlopen")
    def test_collect_happy_path(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")

        # First call is validate (GET), second is search (POST)
        validate_resp = MagicMock()
        validate_resp.read.return_value = b'{"valid": true}'

        logs_resp = MagicMock()
        body = json.dumps(_dd_response([{"message": "error msg"}])).encode("utf-8")
        logs_resp.read.return_value = body

        mock_urlopen.side_effect = [
            validate_resp,
            logs_resp,
        ]

        collector = DatadogCollector()
        collector.validate_config(_dd_config())
        result = collector.collect(_dd_config())

        assert isinstance(result, CollectedData)
        assert result.source == "datadog"
        assert result.data_type == "logs"
        assert result.entry_count == 1
        assert len(result.entries) == 1
        assert result.entries[0]["message"] == "error msg"

    @patch("autopsy.collectors.datadog.urlopen")
    def test_collect_with_service_filter_in_query(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")

        validate_resp = MagicMock()
        validate_resp.read.return_value = b"{}"

        logs_resp = MagicMock()

        def _fake_urlopen(req: Any, timeout: int = 30) -> Any:  # noqa: ARG001
            if req.full_url.endswith("/api/v1/validate"):
                return validate_resp
            body = json.loads(req.data.decode("utf-8"))
            assert "service:svc-api" in body["filter"]["query"]
            logs_resp.read.return_value = json.dumps(_dd_response([])).encode("utf-8")
            return logs_resp

        mock_urlopen.side_effect = _fake_urlopen

        collector = DatadogCollector()
        collector.validate_config(_dd_config())
        with pytest.raises(NoDataError):
            collector.collect(_dd_config(service="svc-api"))

    @patch("autopsy.collectors.datadog.urlopen")
    def test_collect_with_source_filter_in_query(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")

        validate_resp = MagicMock()
        validate_resp.read.return_value = b"{}"

        logs_resp = MagicMock()

        def _fake_urlopen(req: Any, timeout: int = 30) -> Any:  # noqa: ARG001
            if req.full_url.endswith("/api/v1/validate"):
                return validate_resp
            body = json.loads(req.data.decode("utf-8"))
            assert "source:python" in body["filter"]["query"]
            logs_resp.read.return_value = json.dumps(_dd_response([])).encode("utf-8")
            return logs_resp

        mock_urlopen.side_effect = _fake_urlopen

        collector = DatadogCollector()
        collector.validate_config(_dd_config())
        with pytest.raises(NoDataError):
            collector.collect(_dd_config(source="python"))

    @patch("autopsy.collectors.datadog.urlopen")
    def test_collect_empty_results_raises_no_data(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")

        validate_resp = MagicMock()
        validate_resp.read.return_value = b"{}"

        logs_resp = MagicMock()
        logs_resp.read.return_value = json.dumps(_dd_response([])).encode("utf-8")

        mock_urlopen.side_effect = [validate_resp, logs_resp]

        collector = DatadogCollector()
        collector.validate_config(_dd_config())
        with pytest.raises(NoDataError):
            collector.collect(_dd_config(time_window=15))

    @patch("autopsy.collectors.datadog.urlopen")
    def test_collect_pagination_with_cursor(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")

        validate_resp = MagicMock()
        validate_resp.read.return_value = b"{}"

        page1 = MagicMock()
        page1.read.return_value = json.dumps(
            _dd_response(
                [{"message": "m1"}, {"message": "m2"}],
                cursor="next-cursor",
            )
        ).encode("utf-8")

        page2 = MagicMock()
        page2.read.return_value = json.dumps(
            _dd_response(
                [{"message": "m3"}],
                cursor=None,
            )
        ).encode("utf-8")

        mock_urlopen.side_effect = [validate_resp, page1, page2]

        collector = DatadogCollector()
        collector.validate_config(_dd_config())
        result = collector.collect(_dd_config())
        messages = [e["message"] for e in result.entries]
        assert set(messages) == {"m1", "m2", "m3"}

    @patch("autopsy.collectors.datadog.urlopen")
    def test_collect_rate_limit_429_raises(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DD_API_KEY", "key")
        monkeypatch.setenv("DD_APP_KEY", "app")

        validate_resp = MagicMock()
        validate_resp.read.return_value = b"{}"

        mock_urlopen.side_effect = [validate_resp, _http_error(429)]

        collector = DatadogCollector()
        collector.validate_config(_dd_config())
        with pytest.raises(DatadogRateLimitError):
            collector.collect(_dd_config())


class TestNormalization:
    def test_normalize_entry_full(self) -> None:
        raw = {
            "id": "log-id",
            "attributes": {
                "timestamp": "2026-03-11T02:15:33.445Z",
                "message": "NullPointerException",
                "status": "error",
                "service": "svc",
                "host": "host1",
            },
        }
        collector = DatadogCollector()
        out = collector._normalize_entry(raw)
        assert out["timestamp"] == "2026-03-11T02:15:33.445Z"
        assert out["message"] == "NullPointerException"
        assert out["service"] == "svc"
        assert out["host"] == "host1"
        assert out["source"] == "datadog"
        assert out["log_level"] == "error"
        assert out["raw_id"] == "log-id"

    def test_normalize_entry_missing_fields(self) -> None:
        raw = {"id": "id-1", "attributes": {"message": "oops"}}
        collector = DatadogCollector()
        out = collector._normalize_entry(raw)
        assert out["timestamp"] == ""
        assert out["message"] == "oops"
        assert out["service"] == "unknown"
        assert out["host"] == ""


class TestSiteRoutingAndConfig:
    def test_site_us1_url(self) -> None:
        assert SITE_URLS["us1"] == "https://api.datadoghq.com"

    def test_site_eu1_url(self) -> None:
        assert SITE_URLS["eu1"] == "https://api.datadoghq.eu"

    def test_site_invalid_raises_config_validation_error(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            DatadogConfig(site="invalid")  # type: ignore[arg-type]

    def test_datadog_config_optional_on_autopsy_config(self) -> None:
        cfg = AutopsyConfig(
            aws=AWSConfig(region="us-east-1", log_groups=["/aws/lambda/a"]),
            github=GitHubConfig(repo="owner/repo"),
        )
        assert cfg.datadog is None

    def test_datadog_config_present_and_defaults(self) -> None:
        cfg = AutopsyConfig(
            aws=AWSConfig(region="us-east-1", log_groups=["/aws/lambda/a"]),
            github=GitHubConfig(repo="owner/repo"),
            datadog=DatadogConfig(),
        )
        assert cfg.datadog is not None
        assert cfg.datadog.api_key_env == "DD_API_KEY"
        assert cfg.datadog.site == "us1"

