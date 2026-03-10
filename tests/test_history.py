"""Tests for autopsy.history.HistoryStore and CLI integration."""

from __future__ import annotations

import json
import threading
from time import sleep
from typing import TYPE_CHECKING

import pytest

from autopsy.ai.models import (
    CorrelatedDeploy,
    DiagnosisResult,
    RootCause,
    SuggestedFix,
    TimelineEvent,
)
from autopsy.config import AIConfig, AutopsyConfig, AWSConfig, GitHubConfig, OutputConfig
from autopsy.history import HistoryStore
from autopsy.utils.errors import HistoryAmbiguousMatchError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _minimal_config() -> AutopsyConfig:
    return AutopsyConfig(
        aws=AWSConfig(region="us-east-1", log_groups=["/aws/lambda/test"], time_window=30),
        github=GitHubConfig(
            repo="owner/repo",
            token_env="GITHUB_TOKEN",
            deploy_count=5,
            branch="main",
        ),
        ai=AIConfig(provider="anthropic", model="claude-sonnet-4-20250514"),
        output=OutputConfig(),
    )


def _sample_result(summary: str = "Test root cause", author: str = "dev") -> DiagnosisResult:
    return DiagnosisResult(
        root_cause=RootCause(
            summary=summary,
            category="code_change",
            confidence=0.9,
            evidence=["line 1", "line 2"],
        ),
        correlated_deploy=CorrelatedDeploy(
            commit_sha="abc123",
            author=author,
            pr_title="fix: thing",
            changed_files=["src/foo.py"],
        ),
        suggested_fix=SuggestedFix(
            immediate="Revert abc123",
            long_term="Add tests",
        ),
        timeline=[
            TimelineEvent(time="2026-03-06T10:00:00Z", event="Deploy"),
            TimelineEvent(time="2026-03-06T10:05:00Z", event="Error"),
        ],
    )


# ---------------------------------------------------------------------------
# HistoryStore core behaviour
# ---------------------------------------------------------------------------


def test_schema_created_for_fresh_db(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        # Saving should work and implicitly prove that schema exists.
        store.save(
            result=_sample_result(),
            duration_s=1.23,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
    assert db.exists()


def test_save_stores_all_fields(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    result = _sample_result()
    with HistoryStore(db_path=db) as store:
        diagnosis_id = store.save(
            result=result,
            duration_s=2.5,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=45,
        )
        row = store.get(diagnosis_id)

    assert row is not None
    assert row["summary"] == result.root_cause.summary
    assert row["category"] == result.root_cause.category
    assert pytest.approx(row["confidence"], rel=1e-6) == result.root_cause.confidence
    assert json.loads(row["evidence"]) == result.root_cause.evidence
    assert row["commit_sha"] == result.correlated_deploy.commit_sha
    assert row["commit_author"] == result.correlated_deploy.author
    assert row["pr_title"] == result.correlated_deploy.pr_title
    assert json.loads(row["changed_files"]) == result.correlated_deploy.changed_files
    assert row["fix_immediate"] == result.suggested_fix.immediate
    assert row["fix_long_term"] == result.suggested_fix.long_term
    assert json.loads(row["timeline"])[0]["event"] == result.timeline[0].event
    assert json.loads(row["log_groups"]) == ["/aws/lambda/test"]
    assert row["github_repo"] == "owner/repo"
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-sonnet-4-20250514"
    assert row["time_window"] == 45
    # raw_json should be valid diagnosis JSON
    parsed = DiagnosisResult.model_validate_json(row["raw_json"])
    assert parsed.root_cause.summary == result.root_cause.summary


def test_save_generates_valid_uuid(tmp_path: Path) -> None:
    from uuid import UUID

    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        diagnosis_id = store.save(
            result=_sample_result(),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
    # Must be a valid UUID4
    parsed = UUID(diagnosis_id)
    assert parsed.version == 4


def test_list_recent_newest_first_and_pagination(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        ids = []
        for i in range(5):
            ids.append(
                store.save(
                    result=_sample_result(summary=f"Result {i}"),
                    duration_s=float(i),
                    log_groups=["/aws/lambda/test"],
                    github_repo="owner/repo",
                    provider="anthropic",
                    model="claude-sonnet-4-20250514",
                    time_window=30,
                )
            )
        rows = store.list_recent(limit=3)
        assert len(rows) == 3
        # Newest first means last saved first in list
        assert rows[0].summary == "Result 4"
        assert rows[1].summary == "Result 3"
        # Offset
        rows_offset = store.list_recent(limit=2, offset=2)
        assert [r.summary for r in rows_offset] == ["Result 2", "Result 1"]


def test_get_with_full_id_and_prefix_and_not_found(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        diagnosis_id = store.save(
            result=_sample_result(),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        full_row = store.get(diagnosis_id)
        prefix_row = store.get(diagnosis_id[:8])
        missing_row = store.get("deadbeef")

    assert full_row is not None
    assert prefix_row is not None
    assert full_row["id"] == prefix_row["id"]
    assert missing_row is None


def test_get_with_ambiguous_prefix_raises(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        # Create two diagnoses we will make share a fake prefix
        id1 = store.save(
            result=_sample_result(summary="One"),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        id2 = store.save(
            result=_sample_result(summary="Two"),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        # Force them to share a common prefix in the DB layer by truncating.
        shared_prefix = "e4a91bc"
        store._conn.execute(
            "UPDATE diagnoses SET id = ? WHERE id = ?",
            (shared_prefix + "3-xxxx-xxxx-xxxx", id1),
        )
        store._conn.execute(
            "UPDATE diagnoses SET id = ? WHERE id = ?",
            (shared_prefix + "8-xxxx-xxxx-xxxx", id2),
        )
        store._conn.commit()

        with pytest.raises(HistoryAmbiguousMatchError) as excinfo:
            store.get(shared_prefix)

    err = excinfo.value
    assert err.prefix == shared_prefix
    # Candidates should include both
    ids = {c["id"] for c in err.candidates}
    assert any(i.startswith(shared_prefix + "3") for i in ids)
    assert any(i.startswith(shared_prefix + "8") for i in ids)


def test_search_by_summary_and_author_and_no_matches(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        store.save(
            result=_sample_result(
                summary="NullPointerException in handler", author="alice@example.com"
            ),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        store.save(
            result=_sample_result(summary="Timeout in user service", author="bob@example.com"),
            duration_s=2.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )

        by_summary = store.search("NullPointerException")
        by_author = store.search("bob@example.com")
        no_match = store.search("totally unrelated")

    assert len(by_summary) == 1
    assert "NullPointerException" in by_summary[0].summary
    assert len(by_author) == 1
    assert by_author[0].summary.startswith("Timeout")
    assert no_match == []


def test_get_stats_with_data_and_empty(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        empty_stats = store.get_stats()
        assert empty_stats["total"] == 0
        assert empty_stats["top_category"] is None

        store.save(
            result=_sample_result(summary="A", author="alice"),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        store.save(
            result=_sample_result(summary="B", author="bob"),
            duration_s=3.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        stats = store.get_stats()

    assert stats["total"] == 2
    assert stats["top_category"] == "code_change"
    assert stats["top_category_count"] == 2
    assert stats["top_repo"] == "owner/repo"
    assert stats["top_repo_count"] == 2
    assert stats["avg_confidence"] is not None
    assert stats["avg_duration_s"] is not None


def test_delete_and_clear(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    with HistoryStore(db_path=db) as store:
        id1 = store.save(
            result=_sample_result(summary="Delete me"),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        store.save(
            result=_sample_result(summary="Keep until clear"),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )

        assert store.delete(id1) is True
        assert store.get(id1) is None

        count_cleared = store.clear()
        assert count_cleared >= 1
        assert store.list_recent() == []


def test_export_json_and_csv(tmp_path: Path) -> None:
    db = tmp_path / "history.db"
    json_path = tmp_path / "history.json"
    csv_path = tmp_path / "history.csv"

    with HistoryStore(db_path=db) as store:
        store.save(
            result=_sample_result(),
            duration_s=1.0,
            log_groups=["/aws/lambda/test"],
            github_repo="owner/repo",
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            time_window=30,
        )
        n_json = store.export(json_path, fmt="json")
        n_csv = store.export(csv_path, fmt="csv")

    assert n_json == 1
    assert n_csv == 1
    data_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(data_json, list)
    assert data_json[0]["github_repo"] == "owner/repo"
    assert "raw_json" in data_json[0]
    assert csv_path.read_text(encoding="utf-8").startswith("id,created_at")


def test_concurrent_access_does_not_corrupt_db(tmp_path: Path) -> None:
    db = tmp_path / "history.db"

    def _worker(save_fn: Callable[[], None]) -> None:
        for _ in range(5):
            save_fn()
            sleep(0.01)

    with HistoryStore(db_path=db) as store:
        def save_once() -> None:
            store.save(
                result=_sample_result(),
                duration_s=1.0,
                log_groups=["/aws/lambda/test"],
                github_repo="owner/repo",
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                time_window=30,
            )

        threads = [threading.Thread(target=_worker, args=(save_once,)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = store.list_recent(limit=100)

    # 3 threads * 5 saves each
    assert len(rows) == 15


# ---------------------------------------------------------------------------
# Pipeline non-blocking behaviour
# ---------------------------------------------------------------------------


def test_history_save_failure_does_not_break_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """If HistoryStore.save raises, DiagnosisOrchestrator.run still returns."""
    from unittest.mock import MagicMock, patch

    from autopsy.diagnosis import DiagnosisOrchestrator

    config = _minimal_config()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-test")

    with (
        patch("autopsy.diagnosis.CloudWatchCollector") as mock_cw_cls,
        patch("autopsy.diagnosis.GitHubCollector") as mock_gh_cls,
        patch("autopsy.diagnosis.AIEngine") as mock_engine_cls,
        patch("autopsy.history.HistoryStore") as mock_history_cls,
    ):
        mock_cw = mock_cw_cls.return_value
        mock_cw.validate_config.return_value = True
        mock_cw.collect.return_value = MagicMock(source="cloudwatch", data_type="logs")

        mock_gh = mock_gh_cls.return_value
        mock_gh.validate_config.return_value = True
        mock_gh.collect.return_value = MagicMock(source="github", data_type="deploys")

        mock_engine = mock_engine_cls.return_value
        mock_engine.diagnose.return_value = _sample_result()

        mock_store = mock_history_cls.return_value.__enter__.return_value
        mock_store.save.side_effect = RuntimeError("boom")

        orch = DiagnosisOrchestrator(config)
        result = orch.run()

    assert isinstance(result, DiagnosisResult)


# ---------------------------------------------------------------------------
# CLI integration (smoke tests)
# ---------------------------------------------------------------------------


class TestHistoryCLI:
    def test_history_list_empty_and_non_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from autopsy.cli import cli

        # Point DB_PATH to temp dir
        monkeypatch.setattr("autopsy.history.DB_PATH", tmp_path / "history.db", raising=False)

        runner = CliRunner()
        # Empty DB
        result_empty = runner.invoke(cli, ["history", "list"], catch_exceptions=False)
        assert result_empty.exit_code == 0
        assert "No diagnoses saved yet" in result_empty.output

        # Populate one entry
        with HistoryStore(db_path=tmp_path / "history.db") as store:
            store.save(
                result=_sample_result(),
                duration_s=1.0,
                log_groups=["/aws/lambda/test"],
                github_repo="owner/repo",
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                time_window=30,
            )

        result = runner.invoke(cli, ["history", "list"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Diagnosis History" in result.output

    def test_history_show_ambiguous_prefix_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from autopsy.cli import cli

        monkeypatch.setattr("autopsy.history.DB_PATH", tmp_path / "history.db", raising=False)

        shared_prefix = "e4a91bc"
        with HistoryStore(db_path=tmp_path / "history.db") as store:
            id1 = store.save(
                result=_sample_result(summary="NullPointerException in processRefund()"),
                duration_s=1.0,
                log_groups=["/aws/lambda/test"],
                github_repo="owner/repo",
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                time_window=30,
            )
            id2 = store.save(
                result=_sample_result(summary="Timeout in user lookup service"),
                duration_s=1.0,
                log_groups=["/aws/lambda/test"],
                github_repo="owner/repo",
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                time_window=30,
            )
            store._conn.execute(
                "UPDATE diagnoses SET id = ? WHERE id = ?",
                (shared_prefix + "3-xxxx-xxxx-xxxx", id1),
            )
            store._conn.execute(
                "UPDATE diagnoses SET id = ? WHERE id = ?",
                (shared_prefix + "8-xxxx-xxxx-xxxx", id2),
            )
            store._conn.commit()

        runner = CliRunner()
        result = runner.invoke(cli, ["history", "show", shared_prefix], catch_exceptions=False)
        assert result.exit_code == 0
        assert 'Multiple diagnoses match "e4a91bc"' in result.output
        assert "NullPointerException in processRefund()" in result.output
        assert "Timeout in user lookup service" in result.output
        assert "Use the full ID: autopsy history show" in result.output

