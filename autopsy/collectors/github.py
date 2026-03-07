"""GitHub deploys and diffs collector.

Pulls recent commits, diffs, PR metadata, and deployment events using
PyGitHub. Implements smart diff reduction to fit LLM context budgets.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

from github import Auth, Github, GithubException, RateLimitExceededException
from rich.console import Console

from autopsy.collectors.base import BaseCollector, CollectedData
from autopsy.utils.errors import GitHubAuthError, GitHubRateLimitError, NoDataError

console = Console(stderr=True)

INCLUDE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".go",
        ".java",
        ".yaml",
        ".yml",
        ".json",
        ".tf",
    }
)
INCLUDE_FILENAMES: frozenset[str] = frozenset({"Dockerfile"})

_EXCLUDE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+$"),
    re.compile(r"(^|/)[^/]*_test\.[^/]+$"),
    re.compile(r"(^|/)[^/]*\.test\.[^/]+$"),
    re.compile(r"(^|/)[^/]*\.spec\.[^/]+$"),
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"(^|/)yarn\.lock$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)Pipfile\.lock$"),
    re.compile(r"(^|/)pnpm-lock\.yaml$"),
    re.compile(r"(^|/)generated/"),
    re.compile(r"(^|/)__generated__/"),
    re.compile(r"\.g\.[^/]+$"),
    re.compile(r"\.generated\.[^/]+$"),
)

MAX_DIFF_LINES = 200
MAX_FILES_PER_COMMIT = 10


class GitHubCollector(BaseCollector):
    """Collects recent deploys, diffs, and PR metadata from GitHub."""

    @property
    def name(self) -> str:
        """Collector identifier."""
        return "github"

    def validate_config(self, config: dict) -> bool:
        """Verify GitHub PAT and repository access.

        Args:
            config: The 'github' section of AutopsyConfig.

        Returns:
            True if authentication and repo access succeed.

        Raises:
            GitHubAuthError: On invalid or expired PAT.
            GitHubRateLimitError: On API rate limit exceeded.
        """
        token = _resolve_token(config)
        gh = Github(auth=Auth.Token(token))
        repo_name = config.get("repo", "")
        try:
            gh.get_repo(repo_name)
        except RateLimitExceededException as exc:
            raise GitHubRateLimitError(
                message="GitHub API rate limit exceeded.",
                hint="Wait for the rate limit to reset, or use a PAT with higher limits.",
            ) from exc
        except GithubException as exc:
            _raise_for_github_error(exc, repo_name)
        finally:
            gh.close()
        return True

    def collect(self, config: dict) -> CollectedData:
        """Pull recent deploys, diffs, and PR metadata from GitHub.

        Args:
            config: The 'github' section of AutopsyConfig.

        Returns:
            Normalized CollectedData with deploy entries.

        Raises:
            GitHubAuthError: On auth failure.
            GitHubRateLimitError: On rate limit.
            NoDataError: On zero results.
        """
        token = _resolve_token(config)
        gh = Github(auth=Auth.Token(token))
        repo_name = config.get("repo", "")
        branch = config.get("branch", "main")
        deploy_count = config.get("deploy_count", 5)

        try:
            repo = gh.get_repo(repo_name)
        except RateLimitExceededException as exc:
            raise GitHubRateLimitError(
                message="GitHub API rate limit exceeded.",
                hint="Wait for the rate limit to reset, or use a PAT with higher limits.",
            ) from exc
        except GithubException as exc:
            _raise_for_github_error(exc, repo_name)

        try:
            commits_page = repo.get_commits(sha=branch)
            commits = list(commits_page[:deploy_count])
        except RateLimitExceededException as exc:
            raise GitHubRateLimitError(
                message="GitHub API rate limit exceeded while fetching commits.",
                hint="Wait for the rate limit to reset, or use a PAT with higher limits.",
            ) from exc
        except GithubException as exc:
            _raise_for_github_error(exc, repo_name)
        finally:
            gh.close()

        if not commits:
            raise NoDataError(
                message=f"No commits found on branch '{branch}'.",
                hint="Check that the branch exists and has commits.",
            )

        entries: list[dict] = []
        for commit in commits:
            entry = _build_entry(commit)
            entries.append(entry)

        timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]
        if timestamps:
            start = min(datetime.fromisoformat(t) for t in timestamps)
            end = max(datetime.fromisoformat(t) for t in timestamps)
        else:
            now = datetime.now(tz=timezone.utc)
            start = end = now

        return CollectedData(
            source="github",
            data_type="deploys",
            entries=entries,
            time_range=(start, end),
            raw_query=f"last {deploy_count} commits on {branch}",
            entry_count=len(entries),
            truncated=False,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_token(config: dict) -> str:
    """Read the GitHub PAT from the env var specified in config.

    Args:
        config: The 'github' section dict.

    Returns:
        GitHub personal access token string.

    Raises:
        GitHubAuthError: If the env var is missing or empty.
    """
    token_env = config.get("token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env, "")
    if not token:
        raise GitHubAuthError(
            message=f"GitHub token env var '{token_env}' is not set or empty.",
            hint=f"Export {token_env}=ghp_... before running autopsy diagnose.",
        )
    return token


def _raise_for_github_error(exc: GithubException, repo_name: str) -> None:
    """Map a PyGitHub exception to the appropriate Autopsy error.

    Args:
        exc: The caught GithubException.
        repo_name: Repository name for error context.

    Raises:
        GitHubAuthError: On 401/403 responses.
        GitHubRateLimitError: On 429 or rate-limit responses.
        GitHubAuthError: On other errors (fallback).
    """
    status = exc.status if hasattr(exc, "status") else 0

    if status == 401:
        raise GitHubAuthError(
            message="GitHub authentication failed (401).",
            hint="Check that your GITHUB_TOKEN is valid and not expired.",
        ) from exc

    if status == 403:
        raise GitHubAuthError(
            message=f"GitHub access denied (403) for repo '{repo_name}'.",
            hint="Ensure your token has 'repo' scope for private repos.",
        ) from exc

    if status == 404:
        raise GitHubAuthError(
            message=f"GitHub repository not found: '{repo_name}'.",
            hint="Check the repo name (owner/repo) and token permissions.",
        ) from exc

    if status == 429:
        raise GitHubRateLimitError(
            message="GitHub API rate limit exceeded (429).",
            hint="Wait for the rate limit to reset, or use a PAT with higher limits.",
        ) from exc

    raise GitHubAuthError(
        message=f"GitHub API error ({status}): {exc.data}",
        hint="Check your GitHub configuration and token.",
    ) from exc


def _build_entry(commit) -> dict:
    """Build a normalized entry dict from a PyGitHub Commit.

    Fetches the commit's files/diffs, applies smart diff reduction, and
    looks up any associated pull request.

    Args:
        commit: A github.Commit.Commit object.

    Returns:
        Dict with sha, author, timestamp, message, files, diff, and pr metadata.
    """
    sha = commit.sha
    author = commit.commit.author.name if commit.commit.author else "unknown"
    email = commit.commit.author.email if commit.commit.author else ""
    timestamp = (
        commit.commit.author.date.isoformat()
        if commit.commit.author and commit.commit.author.date
        else ""
    )
    message = commit.commit.message or ""

    all_files = list(commit.files) if commit.files else []

    relevant_files = [f for f in all_files if _is_relevant_file(f.filename)]

    if len(relevant_files) > MAX_FILES_PER_COMMIT:
        relevant_files.sort(key=lambda f: f.changes, reverse=True)
        kept = relevant_files[:MAX_FILES_PER_COMMIT]
        omitted = len(relevant_files) - MAX_FILES_PER_COMMIT
        summary = f"(+{omitted} more files omitted, kept top {MAX_FILES_PER_COMMIT} by change size)"
    else:
        kept = relevant_files
        summary = ""

    file_entries: list[dict] = []
    for f in kept:
        diff = _cap_diff(f.patch or "") if f.patch else ""
        file_entries.append(
            {
                "filename": f.filename,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "diff": diff,
            }
        )

    pr_info = _find_pr_for_commit(commit)

    entry: dict = {
        "sha": sha,
        "author": author,
        "email": email,
        "timestamp": timestamp,
        "message": message,
        "files": [f["filename"] for f in file_entries],
        "file_diffs": file_entries,
        "total_files_in_commit": len(all_files),
        "relevant_files_count": len(relevant_files),
    }
    if summary:
        entry["files_summary"] = summary
    if pr_info:
        entry["pr"] = pr_info

    return entry


def _is_relevant_file(filename: str) -> bool:
    """Determine if a file should be included based on extension/name filters.

    Args:
        filename: Path of the file relative to repo root.

    Returns:
        True if the file passes inclusion and exclusion filters.
    """
    path = PurePosixPath(filename)

    for pattern in _EXCLUDE_PATTERNS:
        if pattern.search(filename):
            return False

    if path.name in INCLUDE_FILENAMES:
        return True

    return path.suffix in INCLUDE_EXTENSIONS


def _cap_diff(patch: str) -> str:
    """Cap a diff to MAX_DIFF_LINES.

    Args:
        patch: Raw unified diff string.

    Returns:
        Truncated diff if it exceeds the line limit.
    """
    lines = patch.split("\n")
    if len(lines) <= MAX_DIFF_LINES:
        return patch
    kept = lines[:MAX_DIFF_LINES]
    omitted = len(lines) - MAX_DIFF_LINES
    kept.append(f"... ({omitted} more lines omitted)")
    return "\n".join(kept)


def _find_pr_for_commit(commit) -> dict | None:
    """Look up a pull request associated with a commit.

    Uses the GitHub API to find PRs that contain this commit SHA.
    Returns the first match or None.

    Args:
        commit: A github.Commit.Commit object.

    Returns:
        Dict with pr_number, pr_title, pr_body (truncated) or None.
    """
    try:
        pulls = commit.get_pulls()
        for pr in pulls:
            body = pr.body or ""
            if len(body) > 500:
                body = body[:500] + "…"
            return {
                "number": pr.number,
                "title": pr.title,
                "body": body,
                "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
            }
    except GithubException:
        return None
    return None
