"""Tests for autopsy.collectors.gitlab — GitLab collector."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from gitlab.exceptions import (
    GitlabAuthenticationError,
    GitlabError,
    GitlabGetError,
    GitlabHttpError,
)
from pydantic import ValidationError

from autopsy.collectors.gitlab import (
    GitLabCollector,
    _build_entry,
    _cap_diff,
    _find_mr_for_commit,
    _is_relevant_file,
)
from autopsy.utils.errors import (
    CollectorError,
    GitLabAuthError,
    GitLabRateLimitError,
    NoDataError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gl_config(
    url: str = "https://gitlab.com",
    token_env: str = "GITLAB_TOKEN",
    project_id: str = "12345",
    branch: str = "main",
    deploy_count: int = 5,
) -> dict:
    """Build a minimal GitLab config dict."""
    return {
        "url": url,
        "token_env": token_env,
        "project_id": project_id,
        "branch": branch,
        "deploy_count": deploy_count,
    }


def _mock_diff(
    new_path: str = "src/handler.py",
    old_path: str = "src/handler.py",
    diff: str = "@@ -1,3 +1,5 @@\n+import os\n def main():\n-    pass\n+    run()",
    new_file: bool = False,
    deleted_file: bool = False,
) -> dict:
    """Build a mock GitLab diff dict."""
    return {
        "new_path": new_path,
        "old_path": old_path,
        "diff": diff,
        "new_file": new_file,
        "deleted_file": deleted_file,
    }


def _mock_commit(
    commit_id: str = "abc1234def5678901234567890abcdef12345678",
    author_name: str = "dev",
    author_email: str = "dev@example.com",
    message: str = "fix: null check",
    committed_date: str = "2026-03-06T10:00:00+00:00",
    diffs: list[dict] | None = None,
) -> MagicMock:
    """Build a mock python-gitlab Commit object."""
    if diffs is None:
        diffs = [_mock_diff()]

    commit = MagicMock()
    commit.id = commit_id
    commit.author_name = author_name
    commit.author_email = author_email
    commit.message = message
    commit.committed_date = committed_date
    commit.diff.return_value = diffs
    return commit


def _mock_mr(
    iid: int = 42,
    title: str = "Fix null check in handler",
    description: str = "This MR fixes the null check.",
    web_url: str = "https://gitlab.com/owner/repo/-/merge_requests/42",
    username: str = "dev",
    merged_at: str = "2026-03-06T11:00:00+00:00",
) -> dict:
    """Build a mock GitLab merge request dict (as returned by commit.merge_requests())."""
    return {
        "iid": iid,
        "title": title,
        "description": description,
        "web_url": web_url,
        "author": {"username": username},
        "merged_at": merged_at,
    }


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

    def test_ruby_file_included(self) -> None:
        assert _is_relevant_file("app/models/user.rb") is True

    def test_rust_file_included(self) -> None:
        assert _is_relevant_file("src/main.rs") is True

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

    def test_package_lock_excluded(self) -> None:
        assert _is_relevant_file("package-lock.json") is False

    def test_yarn_lock_excluded(self) -> None:
        assert _is_relevant_file("yarn.lock") is False

    def test_poetry_lock_excluded(self) -> None:
        assert _is_relevant_file("poetry.lock") is False

    def test_gemfile_lock_excluded(self) -> None:
        assert _is_relevant_file("Gemfile.lock") is False

    def test_go_sum_excluded(self) -> None:
        assert _is_relevant_file("go.sum") is False

    def test_generated_dir_excluded(self) -> None:
        assert _is_relevant_file("generated/schema.py") is False

    def test_vendor_dir_excluded(self) -> None:
        assert _is_relevant_file("vendor/github.com/lib/pq/conn.go") is False

    def test_node_modules_excluded(self) -> None:
        assert _is_relevant_file("node_modules/express/index.js") is False

    def test_dist_excluded(self) -> None:
        assert _is_relevant_file("dist/bundle.js") is False

    def test_build_excluded(self) -> None:
        assert _is_relevant_file("build/output.js") is False

    def test_migration_excluded(self) -> None:
        assert _is_relevant_file("migrations/001_create_users.py") is False

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
    """Entry construction from a GitLab commit."""

    def test_basic_entry_fields(self) -> None:
        commit = _mock_commit()
        diffs = [
            {
                "filename": "src/handler.py",
                "status": "modified",
                "additions": 2,
                "deletions": 1,
                "diff": "+import os",
            }
        ]
        entry = _build_entry(commit, diffs, None)
        assert entry["sha"] == "abc1234def5678901234567890abcdef12345678"
        assert entry["short_sha"] == "abc1234"
        assert entry["author"] == "dev"
        assert entry["email"] == "dev@example.com"
        assert entry["message"] == "fix: null check"
        assert "2026-03-06" in entry["timestamp"]
        assert entry["source"] == "gitlab"

    def test_entry_with_mr(self) -> None:
        commit = _mock_commit()
        diffs = []
        mr = {
            "number": 42,
            "title": "Fix handler",
            "body": "Fixes it.",
            "web_url": "https://gitlab.com/mr/42",
            "author": "dev",
            "merged_at": "2026-03-06T11:00:00Z",
        }
        entry = _build_entry(commit, diffs, mr)
        assert entry["pr"]["number"] == 42
        assert entry["pr"]["title"] == "Fix handler"

    def test_entry_no_mr(self) -> None:
        commit = _mock_commit()
        entry = _build_entry(commit, [], None)
        assert "pr" not in entry

    def test_entry_source_is_gitlab(self) -> None:
        commit = _mock_commit()
        entry = _build_entry(commit, [], None)
        assert entry["source"] == "gitlab"

    def test_files_list_populated(self) -> None:
        diffs = [
            {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "diff": ""},
            {"filename": "b.py", "status": "added", "additions": 5, "deletions": 0, "diff": ""},
        ]
        commit = _mock_commit()
        entry = _build_entry(commit, diffs, None)
        assert entry["files"] == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# MR matching
# ---------------------------------------------------------------------------


class TestFindMrForCommit:
    """Merge request lookup for commits."""

    def test_mr_found(self) -> None:
        project = MagicMock()
        commit_obj = MagicMock()
        commit_obj.merge_requests.return_value = [_mock_mr()]
        project.commits.get.return_value = commit_obj

        result = _find_mr_for_commit(project, "abc123")
        assert result is not None
        assert result["number"] == 42
        assert result["title"] == "Fix null check in handler"

    def test_no_mr_returns_none(self) -> None:
        project = MagicMock()
        commit_obj = MagicMock()
        commit_obj.merge_requests.return_value = []
        project.commits.get.return_value = commit_obj

        result = _find_mr_for_commit(project, "abc123")
        assert result is None

    def test_gitlab_error_returns_none(self) -> None:
        project = MagicMock()
        project.commits.get.side_effect = GitlabError("API error")

        result = _find_mr_for_commit(project, "abc123")
        assert result is None

    def test_long_description_truncated(self) -> None:
        project = MagicMock()
        commit_obj = MagicMock()
        mr_data = _mock_mr(description="x" * 1000)
        commit_obj.merge_requests.return_value = [mr_data]
        project.commits.get.return_value = commit_obj

        result = _find_mr_for_commit(project, "abc123")
        assert result is not None
        assert len(result["body"]) == 501  # 500 + ellipsis


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


class TestTokenResolution:
    """Env var resolution for GitLab PAT."""

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITLAB_TOKEN", raising=False)
        collector = GitLabCollector()
        with pytest.raises(GitLabAuthError, match="not set"):
            collector.validate_config(_gl_config())

    def test_empty_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "")
        collector = GitLabCollector()
        with pytest.raises(GitLabAuthError, match="not set"):
            collector.validate_config(_gl_config())


# ---------------------------------------------------------------------------
# GitLabCollector.validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """Auth and project access validation via mocked python-gitlab."""

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_valid_credentials(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test123")
        mock_gl = MagicMock()
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        assert collector.validate_config(_gl_config()) is True
        mock_gl.auth.assert_called_once()
        mock_gl.projects.get.assert_called_once_with("12345")

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_bad_token_raises_auth_error(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-bad")
        mock_gl = MagicMock()
        mock_gl.auth.side_effect = GitlabAuthenticationError("401 Unauthorized")
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(GitLabAuthError, match="authentication failed"):
            collector.validate_config(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_project_not_found(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-ok")
        mock_gl = MagicMock()
        mock_gl.projects.get.side_effect = GitlabGetError("404 Not Found")
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(CollectorError, match="not found"):
            collector.validate_config(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_rate_limit_raises(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-ok")
        mock_gl = MagicMock()
        exc = GitlabHttpError("429 Too Many Requests")
        exc.response_code = 429
        mock_gl.projects.get.side_effect = exc
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(GitLabRateLimitError):
            collector.validate_config(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_connection_error(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-ok")
        mock_gl = MagicMock()
        mock_gl.auth.side_effect = ConnectionError("Connection refused")
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(CollectorError, match="Cannot connect"):
            collector.validate_config(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_self_hosted_url(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        mock_gl = MagicMock()
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        collector.validate_config(
            _gl_config(url="https://gitlab.mycompany.com")
        )
        mock_gl_cls.assert_called_once_with(
            "https://gitlab.mycompany.com", private_token="glpat-test"
        )


# ---------------------------------------------------------------------------
# GitLabCollector.collect
# ---------------------------------------------------------------------------


class TestCollect:
    """Full collect() with mocked python-gitlab."""

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    @patch("autopsy.collectors.gitlab._find_mr_for_commit", return_value=None)
    def test_happy_path(
        self,
        mock_find_mr: MagicMock,
        mock_gl_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        commits = [_mock_commit(commit_id=f"sha{i}" + "0" * 33) for i in range(3)]

        mock_project = MagicMock()
        mock_project.commits.list.return_value = commits
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        result = collector.collect(_gl_config(deploy_count=3))

        assert result.source == "gitlab"
        assert result.data_type == "deploys"
        assert len(result.entries) == 3
        assert result.entry_count == 3

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    @patch("autopsy.collectors.gitlab._find_mr_for_commit")
    def test_collect_with_merge_requests(
        self,
        mock_find_mr: MagicMock,
        mock_gl_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        mr_info = {
            "number": 42,
            "title": "Fix handler",
            "body": "Fixes it.",
            "web_url": "https://gitlab.com/mr/42",
            "author": "dev",
            "merged_at": "2026-03-06T11:00:00Z",
        }
        mock_find_mr.return_value = mr_info

        commits = [_mock_commit()]
        mock_project = MagicMock()
        mock_project.commits.list.return_value = commits
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        result = collector.collect(_gl_config(deploy_count=1))

        assert result.entries[0]["pr"]["title"] == "Fix handler"

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_no_commits_raises_no_data(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        mock_project = MagicMock()
        mock_project.commits.list.return_value = []
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(NoDataError, match="No commits found"):
            collector.collect(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_rate_limit_during_collect(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        mock_project = MagicMock()
        exc = GitlabHttpError("429 Too Many Requests")
        exc.response_code = 429
        mock_project.commits.list.side_effect = exc
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(GitLabRateLimitError):
            collector.collect(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    def test_auth_error_during_collect(
        self, mock_gl_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-bad")
        mock_gl = MagicMock()
        mock_gl.auth.side_effect = GitlabAuthenticationError("401 Unauthorized")
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        with pytest.raises(GitLabAuthError):
            collector.collect(_gl_config())

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    @patch("autopsy.collectors.gitlab._find_mr_for_commit", return_value=None)
    def test_branch_filter(
        self,
        mock_find_mr: MagicMock,
        mock_gl_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        commits = [_mock_commit()]
        mock_project = MagicMock()
        mock_project.commits.list.return_value = commits
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        collector.collect(_gl_config(branch="develop"))
        mock_project.commits.list.assert_called_once_with(
            ref_name="develop", per_page=5, get_all=False
        )

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    @patch("autopsy.collectors.gitlab._find_mr_for_commit", return_value=None)
    def test_deploy_count(
        self,
        mock_find_mr: MagicMock,
        mock_gl_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        commits = [_mock_commit()]
        mock_project = MagicMock()
        mock_project.commits.list.return_value = commits
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        collector.collect(_gl_config(deploy_count=10))
        mock_project.commits.list.assert_called_once_with(
            ref_name="main", per_page=10, get_all=False
        )

    @patch("autopsy.collectors.gitlab.gl_module.Gitlab")
    @patch("autopsy.collectors.gitlab._find_mr_for_commit", return_value=None)
    def test_time_range_from_commits(
        self,
        mock_find_mr: MagicMock,
        mock_gl_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GITLAB_TOKEN", "glpat-test")
        commits = [
            _mock_commit(commit_id="sha1" + "0" * 36, committed_date="2026-03-06T08:00:00+00:00"),
            _mock_commit(commit_id="sha2" + "0" * 36, committed_date="2026-03-06T12:00:00+00:00"),
        ]
        mock_project = MagicMock()
        mock_project.commits.list.return_value = commits
        mock_gl = MagicMock()
        mock_gl.projects.get.return_value = mock_project
        mock_gl_cls.return_value = mock_gl

        collector = GitLabCollector()
        result = collector.collect(_gl_config(deploy_count=2))
        start, end = result.time_range
        assert start.hour == 8
        assert end.hour == 12


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestGitLabConfig:
    """GitLabConfig validation."""

    def test_gitlab_config_optional(self) -> None:
        """AutopsyConfig works without gitlab section."""
        from autopsy.config import AutopsyConfig

        config = AutopsyConfig(
            aws={"region": "us-east-1", "log_groups": ["/aws/lambda/test"]},
            github={"repo": "owner/repo"},
        )
        assert config.gitlab is None

    def test_gitlab_config_valid(self) -> None:
        from autopsy.config import AutopsyConfig

        config = AutopsyConfig(
            aws={"region": "us-east-1", "log_groups": ["/aws/lambda/test"]},
            github={"repo": "owner/repo"},
            gitlab={"project_id": "12345"},
        )
        assert config.gitlab is not None
        assert config.gitlab.project_id == "12345"
        assert config.gitlab.url == "https://gitlab.com"

    def test_url_must_start_with_http(self) -> None:
        from autopsy.config import GitLabConfig

        with pytest.raises(ValidationError, match="http"):
            GitLabConfig(url="ftp://gitlab.com", project_id="123")

    def test_url_trailing_slash_stripped(self) -> None:
        from autopsy.config import GitLabConfig

        cfg = GitLabConfig(url="https://gitlab.com/", project_id="123")
        assert cfg.url == "https://gitlab.com"

    def test_deploy_count_too_low(self) -> None:
        from autopsy.config import GitLabConfig

        with pytest.raises(ValidationError):
            GitLabConfig(project_id="123", deploy_count=0)

    def test_deploy_count_too_high(self) -> None:
        from autopsy.config import GitLabConfig

        with pytest.raises(ValidationError):
            GitLabConfig(project_id="123", deploy_count=51)

    def test_self_hosted_url_accepted(self) -> None:
        from autopsy.config import GitLabConfig

        cfg = GitLabConfig(url="https://gitlab.mycompany.com", project_id="123")
        assert cfg.url == "https://gitlab.mycompany.com"

    def test_github_and_gitlab_coexist(self) -> None:
        from autopsy.config import AutopsyConfig

        config = AutopsyConfig(
            aws={"region": "us-east-1", "log_groups": ["/aws/lambda/test"]},
            github={"repo": "owner/repo"},
            gitlab={"project_id": "myteam/myapp"},
        )
        assert config.github.repo == "owner/repo"
        assert config.gitlab is not None
        assert config.gitlab.project_id == "myteam/myapp"


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestratorIntegration:
    """GitLab collector in the diagnosis orchestrator."""

    def test_gitlab_collector_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from autopsy.config import AutopsyConfig
        from autopsy.diagnosis import DiagnosisOrchestrator

        config = AutopsyConfig(
            aws={"region": "us-east-1", "log_groups": ["/aws/lambda/test"]},
            github={"repo": "owner/repo"},
            gitlab={"project_id": "12345"},
        )
        orchestrator = DiagnosisOrchestrator(config)
        collectors = orchestrator._get_collectors()
        roles = [getattr(c, "_autopsy_role", "") for c in collectors]
        assert "gitlab" in roles

    def test_github_and_gitlab_coexist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from autopsy.config import AutopsyConfig
        from autopsy.diagnosis import DiagnosisOrchestrator

        config = AutopsyConfig(
            aws={"region": "us-east-1", "log_groups": ["/aws/lambda/test"]},
            github={"repo": "owner/repo"},
            gitlab={"project_id": "12345"},
        )
        orchestrator = DiagnosisOrchestrator(config)
        collectors = orchestrator._get_collectors()
        roles = [getattr(c, "_autopsy_role", "") for c in collectors]
        assert "github" in roles
        assert "gitlab" in roles

    def test_no_gitlab_in_config(self) -> None:
        from autopsy.config import AutopsyConfig
        from autopsy.diagnosis import DiagnosisOrchestrator

        config = AutopsyConfig(
            aws={"region": "us-east-1", "log_groups": ["/aws/lambda/test"]},
            github={"repo": "owner/repo"},
        )
        orchestrator = DiagnosisOrchestrator(config)
        collectors = orchestrator._get_collectors()
        roles = [getattr(c, "_autopsy_role", "") for c in collectors]
        assert "gitlab" not in roles

    def test_collector_name_property(self) -> None:
        collector = GitLabCollector()
        assert collector.name == "gitlab"
