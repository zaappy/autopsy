"""Tests for autopsy.collectors.github — GitHub collector."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from github import GithubException, RateLimitExceededException

from autopsy.collectors.github import (
    GitHubCollector,
    _build_entry,
    _cap_diff,
    _is_relevant_file,
)
from autopsy.utils.errors import GitHubAuthError, GitHubRateLimitError, NoDataError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gh_config(
    repo: str = "owner/repo",
    token_env: str = "GITHUB_TOKEN",
    branch: str = "main",
    deploy_count: int = 5,
) -> dict:
    """Build a minimal GitHub config dict."""
    return {
        "repo": repo,
        "token_env": token_env,
        "branch": branch,
        "deploy_count": deploy_count,
    }


def _mock_file(
    filename: str = "src/handler.py",
    status: str = "modified",
    additions: int = 10,
    deletions: int = 2,
    patch: str = "@@ -1,3 +1,5 @@\n+import os\n def main():\n-    pass\n+    run()",
    changes: int = 12,
) -> MagicMock:
    """Build a mock PyGitHub File object."""
    f = MagicMock()
    f.filename = filename
    f.status = status
    f.additions = additions
    f.deletions = deletions
    f.patch = patch
    f.changes = changes
    return f


def _mock_commit(
    sha: str = "abc1234def5678",
    author_name: str = "dev",
    author_email: str = "dev@example.com",
    message: str = "fix: null check",
    date: datetime | None = None,
    files: list | None = None,
    pulls: list | None = None,
) -> MagicMock:
    """Build a mock PyGitHub Commit object."""
    if date is None:
        date = datetime(2026, 3, 6, 10, 0, 0, tzinfo=timezone.utc)
    if files is None:
        files = [_mock_file()]

    commit = MagicMock()
    commit.sha = sha
    commit.commit.author.name = author_name
    commit.commit.author.email = author_email
    commit.commit.author.date = date
    commit.commit.message = message
    commit.files = files

    mock_pulls = MagicMock()
    if pulls:
        mock_pulls.__iter__ = MagicMock(return_value=iter(pulls))
    else:
        mock_pulls.__iter__ = MagicMock(return_value=iter([]))
    commit.get_pulls.return_value = mock_pulls

    return commit


def _mock_pr(
    number: int = 42,
    title: str = "Fix null check in handler",
    body: str = "This PR fixes the null check.",
    merged_at: datetime | None = None,
) -> MagicMock:
    """Build a mock PyGitHub PullRequest object."""
    pr = MagicMock()
    pr.number = number
    pr.title = title
    pr.body = body
    pr.merged_at = merged_at
    return pr


# ---------------------------------------------------------------------------
# File filtering
# ---------------------------------------------------------------------------


class TestIsRelevantFile:
    """Smart diff reduction: inclusion/exclusion filters."""

    def test_python_file_included(self) -> None:
        assert _is_relevant_file("src/handler.py") is True

    def test_javascript_file_included(self) -> None:
        assert _is_relevant_file("app/index.js") is True

    def test_typescript_file_included(self) -> None:
        assert _is_relevant_file("src/utils.ts") is True

    def test_go_file_included(self) -> None:
        assert _is_relevant_file("cmd/main.go") is True

    def test_java_file_included(self) -> None:
        assert _is_relevant_file("src/Main.java") is True

    def test_yaml_file_included(self) -> None:
        assert _is_relevant_file("config/app.yaml") is True

    def test_json_file_included(self) -> None:
        assert _is_relevant_file("package.json") is True

    def test_terraform_file_included(self) -> None:
        assert _is_relevant_file("infra/main.tf") is True

    def test_dockerfile_included(self) -> None:
        assert _is_relevant_file("Dockerfile") is True

    def test_dockerfile_in_subdir_included(self) -> None:
        assert _is_relevant_file("services/api/Dockerfile") is True

    def test_markdown_excluded(self) -> None:
        assert _is_relevant_file("README.md") is False

    def test_image_excluded(self) -> None:
        assert _is_relevant_file("logo.png") is False

    def test_test_dir_excluded(self) -> None:
        assert _is_relevant_file("tests/test_handler.py") is False

    def test_test_prefix_excluded(self) -> None:
        assert _is_relevant_file("test_handler.py") is False

    def test_test_suffix_excluded(self) -> None:
        assert _is_relevant_file("handler_test.go") is False

    def test_spec_suffix_excluded(self) -> None:
        assert _is_relevant_file("handler.spec.ts") is False

    def test_dot_test_suffix_excluded(self) -> None:
        assert _is_relevant_file("handler.test.js") is False

    def test_package_lock_excluded(self) -> None:
        assert _is_relevant_file("package-lock.json") is False

    def test_yarn_lock_excluded(self) -> None:
        assert _is_relevant_file("yarn.lock") is False

    def test_poetry_lock_excluded(self) -> None:
        assert _is_relevant_file("poetry.lock") is False

    def test_generated_dir_excluded(self) -> None:
        assert _is_relevant_file("generated/schema.py") is False

    def test_generated_suffix_excluded(self) -> None:
        assert _is_relevant_file("api.generated.ts") is False


# ---------------------------------------------------------------------------
# Diff capping
# ---------------------------------------------------------------------------


class TestCapDiff:
    """Cap each file diff at MAX_DIFF_LINES."""

    def test_short_diff_unchanged(self) -> None:
        diff = "@@ -1,3 +1,5 @@\n+import os\n def main():"
        assert _cap_diff(diff) == diff

    def test_long_diff_capped(self) -> None:
        lines = [f"+line {i}" for i in range(300)]
        diff = "\n".join(lines)
        result = _cap_diff(diff)
        result_lines = result.split("\n")
        assert len(result_lines) == 201  # 200 kept + 1 summary
        assert "100 more lines omitted" in result_lines[-1]


# ---------------------------------------------------------------------------
# Build entry
# ---------------------------------------------------------------------------


class TestBuildEntry:
    """Entry construction from a PyGitHub commit."""

    def test_basic_entry_fields(self) -> None:
        commit = _mock_commit()
        entry = _build_entry(commit)
        assert entry["sha"] == "abc1234def5678"
        assert entry["author"] == "dev"
        assert entry["email"] == "dev@example.com"
        assert entry["message"] == "fix: null check"
        assert "2026-03-06" in entry["timestamp"]
        assert len(entry["files"]) == 1

    def test_irrelevant_files_filtered(self) -> None:
        files = [
            _mock_file(filename="src/handler.py"),
            _mock_file(filename="README.md"),
            _mock_file(filename="tests/test_handler.py"),
        ]
        commit = _mock_commit(files=files)
        entry = _build_entry(commit)
        assert entry["files"] == ["src/handler.py"]
        assert entry["total_files_in_commit"] == 3
        assert entry["relevant_files_count"] == 1

    def test_many_files_capped_at_10(self) -> None:
        files = [_mock_file(filename=f"src/mod{i}.py", changes=i) for i in range(15)]
        commit = _mock_commit(files=files)
        entry = _build_entry(commit)
        assert len(entry["files"]) == 10
        assert "files_summary" in entry
        assert "5 more files omitted" in entry["files_summary"]

    def test_large_files_sorted_first(self) -> None:
        files = [_mock_file(filename=f"src/mod{i}.py", changes=i) for i in range(15)]
        commit = _mock_commit(files=files)
        entry = _build_entry(commit)
        changes = [d["additions"] + d["deletions"] for d in entry["file_diffs"]]
        # top-10 by changes means descending order in the kept set
        assert changes[0] >= changes[-1]

    def test_pr_metadata_attached(self) -> None:
        pr = _mock_pr(number=42, title="Fix handler", merged_at=None)
        commit = _mock_commit(pulls=[pr])
        entry = _build_entry(commit)
        assert entry["pr"]["number"] == 42
        assert entry["pr"]["title"] == "Fix handler"

    def test_no_pr_field_when_absent(self) -> None:
        commit = _mock_commit(pulls=[])
        entry = _build_entry(commit)
        assert "pr" not in entry

    def test_long_pr_body_truncated(self) -> None:
        pr = _mock_pr(body="x" * 1000)
        commit = _mock_commit(pulls=[pr])
        entry = _build_entry(commit)
        assert len(entry["pr"]["body"]) == 501  # 500 + ellipsis

    def test_diff_capped_per_file(self) -> None:
        long_patch = "\n".join(f"+line {i}" for i in range(300))
        files = [_mock_file(patch=long_patch)]
        commit = _mock_commit(files=files)
        entry = _build_entry(commit)
        diff_lines = entry["file_diffs"][0]["diff"].split("\n")
        assert len(diff_lines) == 201

    def test_no_files_yields_empty_lists(self) -> None:
        commit = _mock_commit(files=[])
        entry = _build_entry(commit)
        assert entry["files"] == []
        assert entry["file_diffs"] == []


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


class TestTokenResolution:
    """Env var resolution for GitHub PAT."""

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        collector = GitHubCollector()
        with pytest.raises(GitHubAuthError, match="not set or empty"):
            collector.validate_config(_gh_config())

    def test_empty_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "")
        collector = GitHubCollector()
        with pytest.raises(GitHubAuthError, match="not set or empty"):
            collector.validate_config(_gh_config())


# ---------------------------------------------------------------------------
# GitHubCollector.validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """Auth and repo access validation via mocked PyGitHub."""

    @patch("autopsy.collectors.github.Github")
    def test_valid_credentials(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test123")
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        assert collector.validate_config(_gh_config()) is True
        mock_gh.get_repo.assert_called_once_with("owner/repo")

    @patch("autopsy.collectors.github.Github")
    def test_401_raises_auth_error(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_bad")
        mock_gh = MagicMock()
        exc = GithubException(401, {"message": "Bad credentials"}, None)
        mock_gh.get_repo.side_effect = exc
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        with pytest.raises(GitHubAuthError, match="401"):
            collector.validate_config(_gh_config())

    @patch("autopsy.collectors.github.Github")
    def test_404_raises_auth_error(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_ok")
        mock_gh = MagicMock()
        exc = GithubException(404, {"message": "Not Found"}, None)
        mock_gh.get_repo.side_effect = exc
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        with pytest.raises(GitHubAuthError, match="not found"):
            collector.validate_config(_gh_config())

    @patch("autopsy.collectors.github.Github")
    def test_rate_limit_raises(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_ok")
        mock_gh = MagicMock()
        exc = RateLimitExceededException(403, {"message": "rate limit"}, {"Retry-After": "60"})
        mock_gh.get_repo.side_effect = exc
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        with pytest.raises(GitHubRateLimitError):
            collector.validate_config(_gh_config())


# ---------------------------------------------------------------------------
# GitHubCollector.collect
# ---------------------------------------------------------------------------


class TestCollect:
    """Full collect() with mocked PyGitHub."""

    @patch("autopsy.collectors.github.Github")
    def test_happy_path(self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        commits = [_mock_commit(sha=f"sha{i}") for i in range(3)]

        mock_repo = MagicMock()
        mock_page = MagicMock()
        mock_page.__getitem__ = MagicMock(return_value=commits)
        mock_repo.get_commits.return_value = mock_page
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        result = collector.collect(_gh_config(deploy_count=3))

        assert result.source == "github"
        assert result.data_type == "deploys"
        assert len(result.entries) == 3
        assert result.entry_count == 3

    @patch("autopsy.collectors.github.Github")
    def test_no_commits_raises_no_data(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        mock_repo = MagicMock()
        mock_page = MagicMock()
        mock_page.__getitem__ = MagicMock(return_value=[])
        mock_repo.get_commits.return_value = mock_page
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        with pytest.raises(NoDataError, match="No commits found"):
            collector.collect(_gh_config())

    @patch("autopsy.collectors.github.Github")
    def test_rate_limit_during_collect(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        mock_repo = MagicMock()
        exc = RateLimitExceededException(403, {"message": "rate limit"}, {"Retry-After": "60"})
        mock_repo.get_commits.side_effect = exc
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        with pytest.raises(GitHubRateLimitError):
            collector.collect(_gh_config())

    @patch("autopsy.collectors.github.Github")
    def test_auth_error_during_get_repo(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_bad")
        mock_gh = MagicMock()
        exc = GithubException(401, {"message": "Bad credentials"}, None)
        mock_gh.get_repo.side_effect = exc
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        with pytest.raises(GitHubAuthError):
            collector.collect(_gh_config())

    @patch("autopsy.collectors.github.Github")
    def test_time_range_from_commits(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        early = datetime(2026, 3, 6, 8, 0, 0, tzinfo=timezone.utc)
        late = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)
        commits = [
            _mock_commit(sha="sha1", date=early),
            _mock_commit(sha="sha2", date=late),
        ]

        mock_repo = MagicMock()
        mock_page = MagicMock()
        mock_page.__getitem__ = MagicMock(return_value=commits)
        mock_repo.get_commits.return_value = mock_page
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        result = collector.collect(_gh_config(deploy_count=2))
        start, end = result.time_range
        assert start.hour == 8
        assert end.hour == 12

    @patch("autopsy.collectors.github.Github")
    def test_branch_passed_to_get_commits(
        self, mock_gh_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        commits = [_mock_commit()]

        mock_repo = MagicMock()
        mock_page = MagicMock()
        mock_page.__getitem__ = MagicMock(return_value=commits)
        mock_repo.get_commits.return_value = mock_page
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_gh_cls.return_value = mock_gh

        collector = GitHubCollector()
        collector.collect(_gh_config(branch="develop"))
        mock_repo.get_commits.assert_called_once_with(sha="develop")
