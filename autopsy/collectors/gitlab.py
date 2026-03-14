"""GitLab deploys and diffs collector.

Pulls recent commits, diffs, merge request metadata, and deployment events
using python-gitlab. Implements smart diff reduction to fit LLM context budgets.

Sibling of the GitHub collector — shares the same file filtering rules and
diff capping logic (see INCLUDE_EXTENSIONS, _EXCLUDE_PATTERNS, MAX_DIFF_LINES).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath

import gitlab as gl_module
from gitlab.exceptions import (
    GitlabAuthenticationError,
    GitlabError,
    GitlabGetError,
    GitlabHttpError,
)
from rich.console import Console

from autopsy.collectors.base import BaseCollector, CollectedData
from autopsy.utils.errors import (
    CollectorError,
    GitLabAuthError,
    GitLabRateLimitError,
    NoDataError,
)

console = Console(stderr=True)

# ---------------------------------------------------------------------------
# File filtering — mirrors github.py (see sibling for rationale)
# ---------------------------------------------------------------------------

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
        ".rb",
        ".rs",
        ".kt",
        ".swift",
        ".c",
        ".cpp",
        ".h",
    }
)
INCLUDE_FILENAMES: frozenset[str] = frozenset({"Dockerfile"})

_EXCLUDE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Test files
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+$"),
    re.compile(r"(^|/)[^/]*_test\.[^/]+$"),
    re.compile(r"(^|/)[^/]*\.test\.[^/]+$"),
    re.compile(r"(^|/)[^/]*\.spec\.[^/]+$"),
    # Lock files
    re.compile(r"(^|/)package-lock\.json$"),
    re.compile(r"(^|/)yarn\.lock$"),
    re.compile(r"(^|/)poetry\.lock$"),
    re.compile(r"(^|/)Pipfile\.lock$"),
    re.compile(r"(^|/)pnpm-lock\.yaml$"),
    re.compile(r"(^|/)Gemfile\.lock$"),
    re.compile(r"(^|/)go\.sum$"),
    # Generated / vendored
    re.compile(r"(^|/)generated/"),
    re.compile(r"(^|/)__generated__/"),
    re.compile(r"(^|/)vendor/"),
    re.compile(r"(^|/)node_modules/"),
    re.compile(r"(^|/)dist/"),
    re.compile(r"(^|/)build/"),
    re.compile(r"\.g\.[^/]+$"),
    re.compile(r"\.generated\.[^/]+$"),
    # Migrations
    re.compile(r"(^|/)migrations?/"),
)

MAX_DIFF_LINES = 200
MAX_FILES_PER_COMMIT = 10


class GitLabCollector(BaseCollector):
    """Collects recent deploys, diffs, and MR metadata from GitLab."""

    @property
    def name(self) -> str:
        """Collector identifier."""
        return "gitlab"

    def validate_config(self, config: dict) -> bool:
        """Verify GitLab token and project access.

        Args:
            config: The 'gitlab' section of AutopsyConfig.

        Returns:
            True if authentication and project access succeed.

        Raises:
            GitLabAuthError: On invalid or expired token.
            GitLabRateLimitError: On API rate limit exceeded.
            CollectorError: On connection or project-not-found errors.
        """
        url, token = _resolve_connection(config)
        gl = gl_module.Gitlab(url, private_token=token)
        project_id = config.get("project_id", "")

        try:
            gl.auth()
        except GitlabAuthenticationError as exc:
            raise GitLabAuthError(
                message="GitLab authentication failed.",
                hint=(
                    f"Check your GITLAB_TOKEN env var.\n"
                    f"Generate a token at {url}/-/user_settings/personal_access_tokens\n"
                    f"Required scope: read_api"
                ),
            ) from exc
        except (OSError, GitlabError) as exc:
            raise CollectorError(
                message=f"Cannot connect to GitLab at {url}.",
                hint=(
                    f"Verify url in config: '{url}'\n"
                    f"For self-hosted: check VPN connection and URL scheme (https://)."
                ),
            ) from exc

        try:
            gl.projects.get(project_id)
        except GitlabGetError as exc:
            raise CollectorError(
                message=f"GitLab project not found: '{project_id}'.",
                hint=(
                    "Check project_id in config. Use numeric project ID "
                    "(Settings → General) or 'namespace/project' path "
                    "(e.g., 'myteam/myapp')."
                ),
            ) from exc
        except GitlabHttpError as exc:
            _raise_for_http_error(exc, url)
        except GitlabError as exc:
            raise CollectorError(
                message=f"GitLab API error: {exc}",
                hint="Check your GitLab configuration and token.",
            ) from exc

        return True

    def collect(self, config: dict) -> CollectedData:
        """Pull recent deploys, diffs, and MR metadata from GitLab.

        Args:
            config: The 'gitlab' section of AutopsyConfig.

        Returns:
            Normalized CollectedData with deploy entries.

        Raises:
            GitLabAuthError: On auth failure.
            GitLabRateLimitError: On rate limit.
            CollectorError: On connection or API errors.
            NoDataError: On zero results.
        """
        url, token = _resolve_connection(config)
        project_id = config.get("project_id", "")
        branch = config.get("branch", "main")
        deploy_count = config.get("deploy_count", 5)

        gl = gl_module.Gitlab(url, private_token=token)

        try:
            gl.auth()
        except GitlabAuthenticationError as exc:
            raise GitLabAuthError(
                message="GitLab authentication failed.",
                hint=(
                    f"Check your GITLAB_TOKEN env var.\n"
                    f"Generate a token at {url}/-/user_settings/personal_access_tokens\n"
                    f"Required scope: read_api"
                ),
            ) from exc
        except (OSError, GitlabError) as exc:
            raise CollectorError(
                message=f"Cannot connect to GitLab at {url}.",
                hint=(
                    f"Verify url in config: '{url}'\n"
                    f"For self-hosted: check VPN connection and URL scheme (https://)."
                ),
            ) from exc

        try:
            project = gl.projects.get(project_id)
        except GitlabGetError as exc:
            raise CollectorError(
                message=f"GitLab project not found: '{project_id}'.",
                hint=(
                    "Check project_id in config. Use numeric project ID "
                    "(Settings → General) or 'namespace/project' path."
                ),
            ) from exc
        except GitlabHttpError as exc:
            _raise_for_http_error(exc, url)
        except GitlabError as exc:
            raise CollectorError(
                message=f"GitLab API error: {exc}",
                hint="Check your GitLab configuration and token.",
            ) from exc

        with console.status(
            f"Pulling commits from GitLab: {project_id}...", spinner="dots"
        ):
            try:
                commits = project.commits.list(
                    ref_name=branch,
                    per_page=deploy_count,
                    get_all=False,
                )
            except GitlabHttpError as exc:
                _raise_for_http_error(exc, url)
            except GitlabError as exc:
                raise CollectorError(
                    message=f"GitLab API error fetching commits: {exc}",
                    hint="Check your GitLab configuration and token.",
                ) from exc

            if not commits:
                raise NoDataError(
                    message=f"No commits found on branch '{branch}'.",
                    hint="Check that the branch exists and has commits.",
                )

            # Build a SHA→MR index from recently merged MRs for fast lookup
            mr_index = _build_mr_index(project, branch, deploy_count)

            # Attempt to get deployment events (not all projects use these)
            deploy_events = _list_deployments(project, deploy_count)

            entries: list[dict] = []
            for commit in commits:
                diffs = _get_commit_diffs(commit)
                sha = commit.id  # type: ignore[union-attr]
                mr = mr_index.get(sha) or _find_mr_for_commit(project, sha)
                entry = _build_entry(commit, diffs, mr)
                entries.append(entry)

            # Attach deployment metadata if available
            if deploy_events:
                _enrich_with_deployments(entries, deploy_events)

        timestamps = [e["timestamp"] for e in entries if e.get("timestamp")]
        if timestamps:
            start = min(datetime.fromisoformat(t) for t in timestamps)
            end = max(datetime.fromisoformat(t) for t in timestamps)
        else:
            now = datetime.now(tz=timezone.utc)
            start = end = now

        return CollectedData(
            source="gitlab",
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


def _resolve_connection(config: dict) -> tuple[str, str]:
    """Resolve GitLab URL and token from config.

    Args:
        config: The 'gitlab' section dict.

    Returns:
        (url, token) tuple.

    Raises:
        GitLabAuthError: If the env var is missing or empty.
    """
    url = config.get("url", "https://gitlab.com").rstrip("/")
    token_env = config.get("token_env", "GITLAB_TOKEN")
    token = os.environ.get(token_env, "")
    if not token:
        raise GitLabAuthError(
            message=f"GitLab token not found: env var '{token_env}' is not set.",
            hint=f"Run 'autopsy init' to configure, or export {token_env}=glpat-xxxxx",
        )
    return url, token


def _raise_for_http_error(exc: GitlabHttpError, url: str) -> None:
    """Map a GitLab HTTP error to the appropriate Autopsy error.

    Args:
        exc: The caught GitlabHttpError.
        url: GitLab URL for error context.

    Raises:
        GitLabRateLimitError: On 429 responses.
        CollectorError: On other HTTP errors.
    """
    status = getattr(exc, "response_code", 0)
    if status == 429:
        raise GitLabRateLimitError(
            message="GitLab API rate limit exceeded.",
            hint="Wait 60 seconds and retry. Self-hosted GitLab may have custom rate limits.",
        ) from exc
    raise CollectorError(
        message=f"GitLab HTTP error ({status}): {exc}",
        hint=f"Check your GitLab configuration at {url}.",
    ) from exc


def _build_mr_index(
    project: object, branch: str, limit: int
) -> dict[str, dict]:
    """Build a SHA → MR-info index from recently merged MRs.

    Fetches merged MRs targeting the branch and indexes their merge
    commit SHAs for O(1) lookup during entry building.

    Args:
        project: A gitlab.v4.objects.Project object.
        branch: Target branch name.
        limit: Maximum MRs to fetch.

    Returns:
        Dict mapping merge commit SHA to MR info dict.
    """
    index: dict[str, dict] = {}
    try:
        mrs = project.mergerequests.list(  # type: ignore[union-attr]
            state="merged",
            target_branch=branch,
            order_by="updated_at",
            sort="desc",
            per_page=limit,
            get_all=False,
        )
        for mr in mrs:
            sha = getattr(mr, "merge_commit_sha", None)
            if sha:
                body = getattr(mr, "description", "") or ""
                if len(body) > 500:
                    body = body[:500] + "\u2026"
                index[sha] = {
                    "number": getattr(mr, "iid", 0),
                    "title": getattr(mr, "title", ""),
                    "body": body,
                    "web_url": getattr(mr, "web_url", ""),
                    "author": getattr(
                        getattr(mr, "author", None), "username", ""
                    ),
                    "merged_at": getattr(mr, "merged_at", None),
                }
    except GitlabError:
        pass
    return index


def _list_deployments(project: object, limit: int) -> list[dict]:
    """List recent deployment events (best-effort).

    Not all GitLab projects have deployments enabled, so failures are
    silently ignored.

    Args:
        project: A gitlab.v4.objects.Project object.
        limit: Maximum deployments to fetch.

    Returns:
        List of deployment dicts, or empty list if unavailable.
    """
    try:
        return list(
            project.deployments.list(  # type: ignore[union-attr]
                per_page=limit,
                order_by="created_at",
                sort="desc",
                get_all=False,
            )
        )
    except GitlabError:
        return []


def _enrich_with_deployments(
    entries: list[dict], deploy_events: list[object]
) -> None:
    """Attach deployment environment info to matching commit entries.

    Matches deployments to entries by commit SHA. Modifies entries
    in-place.

    Args:
        entries: List of commit entry dicts.
        deploy_events: Deployment objects from the GitLab API.
    """
    sha_to_entry: dict[str, dict] = {e["sha"]: e for e in entries}
    for dep in deploy_events:
        sha = getattr(getattr(dep, "sha", None), "__str__", lambda: "")()
        if not sha:
            sha = getattr(dep, "sha", "")
        if sha in sha_to_entry:
            sha_to_entry[sha]["deployment"] = {
                "environment": getattr(dep, "environment", "unknown"),
                "status": getattr(dep, "status", "unknown"),
                "created_at": getattr(dep, "created_at", ""),
            }


def _get_commit_diffs(commit: object) -> list[dict]:
    """Fetch and filter diffs for a GitLab commit.

    Args:
        commit: A gitlab.v4.objects.ProjectCommit object.

    Returns:
        List of filtered, capped diff dicts.
    """
    try:
        raw_diffs = commit.diff()  # type: ignore[union-attr]
    except GitlabError:
        return []

    relevant = [d for d in raw_diffs if _is_relevant_file(d.get("new_path", d.get("old_path", "")))]

    if len(relevant) > MAX_FILES_PER_COMMIT:
        relevant.sort(key=lambda d: len(d.get("diff", "")), reverse=True)
        kept = relevant[:MAX_FILES_PER_COMMIT]
        omitted = len(relevant) - MAX_FILES_PER_COMMIT
    else:
        kept = relevant
        omitted = 0

    file_entries: list[dict] = []
    for d in kept:
        filename = d.get("new_path", d.get("old_path", ""))
        diff_text = _cap_diff(d.get("diff", ""))
        additions, deletions = _count_diff_changes(d.get("diff", ""))

        if d.get("new_file"):
            status = "added"
        elif d.get("deleted_file"):
            status = "deleted"
        else:
            status = "modified"

        file_entries.append(
            {
                "filename": filename,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "diff": diff_text,
            }
        )

    if omitted > 0 and file_entries:
        file_entries[-1]["_omitted_count"] = omitted

    return file_entries


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


def _count_diff_changes(diff_text: str) -> tuple[int, int]:
    """Count additions and deletions from a unified diff string.

    Args:
        diff_text: Raw unified diff string.

    Returns:
        (additions, deletions) tuple.
    """
    additions = 0
    deletions = 0
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return additions, deletions


def _build_entry(commit: object, diffs: list[dict], mr: dict | None) -> dict:
    """Build a normalized entry dict from a GitLab commit.

    Args:
        commit: A gitlab.v4.objects.ProjectCommit object.
        diffs: Filtered and capped diff entries.
        mr: Associated merge request info, or None.

    Returns:
        Dict with sha, author, timestamp, message, files, diff, and MR metadata.
    """
    sha = commit.id  # type: ignore[union-attr]
    author = commit.author_name  # type: ignore[union-attr]
    email = commit.author_email  # type: ignore[union-attr]
    timestamp = commit.committed_date  # type: ignore[union-attr]
    message = (commit.message or "").strip()  # type: ignore[union-attr]

    # Count omitted files for summary
    omitted = 0
    for d in diffs:
        omitted += d.pop("_omitted_count", 0)

    entry: dict = {
        "sha": sha,
        "short_sha": sha[:7] if sha else "",
        "author": author,
        "email": email,
        "timestamp": timestamp,
        "message": message,
        "files": [d["filename"] for d in diffs],
        "file_diffs": diffs,
        "total_files_in_commit": len(diffs) + omitted,
        "relevant_files_count": len(diffs),
        "source": "gitlab",
    }

    if omitted > 0:
        entry["files_summary"] = (
            f"(+{omitted} more files omitted, kept top {MAX_FILES_PER_COMMIT} by diff size)"
        )

    if mr:
        entry["pr"] = mr

    return entry


def _find_mr_for_commit(project: object, commit_sha: str) -> dict | None:
    """Find the merge request that introduced this commit.

    Args:
        project: A gitlab.v4.objects.Project object.
        commit_sha: Full commit SHA.

    Returns:
        Dict with MR title, description, web_url, author, merged_at or None.
    """
    try:
        commit_obj = project.commits.get(commit_sha)  # type: ignore[union-attr]
        mrs = commit_obj.merge_requests()
        if mrs:
            mr = mrs[0]
            body = mr.get("description", "") or ""
            if len(body) > 500:
                body = body[:500] + "…"
            return {
                "number": mr.get("iid", 0),
                "title": mr.get("title", ""),
                "body": body,
                "web_url": mr.get("web_url", ""),
                "author": mr.get("author", {}).get("username", ""),
                "merged_at": mr.get("merged_at"),
            }
    except GitlabError:
        pass
    return None
