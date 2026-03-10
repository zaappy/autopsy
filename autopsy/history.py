"""Local diagnosis history (SQLite).

Stores past DiagnosisResult outputs under ~/.autopsy/history.db.
Best-effort persistence: history should never block live diagnosis output.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from autopsy.config import CONFIG_DIR
from autopsy.utils.errors import HistoryAmbiguousMatchError, HistoryError

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from autopsy.ai.models import DiagnosisResult

DB_PATH = CONFIG_DIR / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnoses (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    duration_s      REAL,

    summary         TEXT NOT NULL,
    category        TEXT NOT NULL,
    confidence      REAL NOT NULL,
    evidence        TEXT,

    commit_sha      TEXT,
    commit_author   TEXT,
    pr_title        TEXT,
    changed_files   TEXT,

    fix_immediate   TEXT,
    fix_long_term   TEXT,

    timeline        TEXT,

    log_groups      TEXT,
    github_repo     TEXT,
    provider        TEXT,
    model           TEXT,
    prompt_version  TEXT,
    time_window     INTEGER,

    raw_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_created_at ON diagnoses(created_at);
CREATE INDEX IF NOT EXISTS idx_category ON diagnoses(category);
CREATE INDEX IF NOT EXISTS idx_github_repo ON diagnoses(github_repo);
""".strip()


@dataclass(frozen=True)
class DiagnosisSummaryRow:
    id: str
    created_at: str
    summary: str
    category: str
    confidence: float
    commit_sha: str | None
    github_repo: str | None
    duration_s: float | None


def _utc_now_iso() -> str:
    # Milliseconds keep ordering stable for rapid consecutive saves.
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _to_json_array(items: Iterable[Any] | None) -> str | None:
    if items is None:
        return None
    data = list(items)
    return _json_dumps(data)


class HistoryStore:
    """Local SQLite storage for past diagnoses."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                str(self.db_path),
                timeout=30,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise HistoryError(
                message=f"Failed to open history database: {self.db_path}",
                hint="Check filesystem permissions and available disk space.",
            ) from exc

        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._create_tables()

    def __enter__(self) -> HistoryStore:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _apply_pragmas(self) -> None:
        try:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL;")
                self._conn.execute("PRAGMA synchronous=NORMAL;")
                self._conn.execute("PRAGMA foreign_keys=ON;")
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to initialize history database settings.",
                hint="Try deleting ~/.autopsy/history.db if it is corrupted.",
            ) from exc

    def _create_tables(self) -> None:
        try:
            with self._lock:
                self._conn.executescript(_SCHEMA)
                self._conn.commit()
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to create history database schema.",
                hint="Try deleting ~/.autopsy/history.db if it is corrupted.",
            ) from exc

    def save(
        self,
        *,
        result: DiagnosisResult,
        duration_s: float,
        log_groups: list[str],
        github_repo: str,
        provider: str,
        model: str,
        time_window: int,
    ) -> str:
        """Save a diagnosis. Returns the generated UUID."""
        rc = result.root_cause
        cd = result.correlated_deploy
        sf = result.suggested_fix

        base_row = {
            "duration_s": float(duration_s),
            "summary": rc.summary,
            "category": rc.category,
            "confidence": float(rc.confidence),
            "evidence": _to_json_array(rc.evidence),
            "commit_sha": cd.commit_sha,
            "commit_author": cd.author,
            "pr_title": cd.pr_title,
            "changed_files": _to_json_array(cd.changed_files),
            "fix_immediate": sf.immediate,
            "fix_long_term": sf.long_term,
            "timeline": _to_json_array([ev.model_dump() for ev in result.timeline]),
            "log_groups": _to_json_array(log_groups),
            "github_repo": github_repo,
            "provider": provider,
            "model": model,
            "prompt_version": result.prompt_version,
            "time_window": int(time_window),
            "raw_json": result.model_dump_json(),
        }

        last_error: sqlite3.Error | None = None
        for _ in range(3):
            diagnosis_id = str(uuid.uuid4())
            created_at = _utc_now_iso()
            row = {"id": diagnosis_id, "created_at": created_at, **base_row}

            try:
                with self._lock:
                    self._conn.execute(
                        """
                        INSERT INTO diagnoses (
                            id, created_at, duration_s,
                            summary, category, confidence, evidence,
                            commit_sha, commit_author, pr_title, changed_files,
                            fix_immediate, fix_long_term,
                            timeline,
                            log_groups, github_repo, provider, model, prompt_version, time_window,
                            raw_json
                        )
                        VALUES (
                            :id, :created_at, :duration_s,
                            :summary, :category, :confidence, :evidence,
                            :commit_sha, :commit_author, :pr_title, :changed_files,
                            :fix_immediate, :fix_long_term,
                            :timeline,
                            :log_groups, :github_repo, :provider, :model,
                            :prompt_version, :time_window,
                            :raw_json
                        )
                        """,
                        row,
                    )
                    self._conn.commit()
                return diagnosis_id
            except sqlite3.IntegrityError as exc:
                # Extremely rare UUID collision or race; retry with a new UUID.
                last_error = exc
                continue
            except sqlite3.Error as exc:
                raise HistoryError(
                    message="Failed to save diagnosis history.",
                    hint="Check disk space and database permissions.",
                ) from exc

        # If we exhausted retries on IntegrityError, surface as HistoryError.
        raise HistoryError(
            message="Failed to save diagnosis history after multiple attempts.",
            hint="Check for database corruption or remove ~/.autopsy/history.db.",
        ) from last_error

    def list_recent(self, *, limit: int = 20, offset: int = 0) -> list[DiagnosisSummaryRow]:
        """Return recent diagnoses, newest first. Summary view."""
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    SELECT
                        id,
                        created_at,
                        summary,
                        category,
                        confidence,
                        commit_sha,
                        github_repo,
                        duration_s
                    FROM diagnoses
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (int(limit), int(offset)),
                )
                rows = cur.fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to list diagnosis history.",
                hint="Try 'autopsy history clear' if your DB is corrupted.",
            ) from exc

        return [
            DiagnosisSummaryRow(
                id=r["id"],
                created_at=r["created_at"],
                summary=r["summary"],
                category=r["category"],
                confidence=float(r["confidence"]),
                commit_sha=r["commit_sha"],
                github_repo=r["github_repo"],
                duration_s=(float(r["duration_s"]) if r["duration_s"] is not None else None),
            )
            for r in rows
        ]

    def get(self, diagnosis_id: str) -> dict[str, Any] | None:
        """Get full diagnosis by ID.

        Accepts full UUID or prefix. If prefix matches multiple diagnoses,
        raises HistoryAmbiguousMatchError with candidates.
        """
        resolved = self._resolve_id(diagnosis_id)
        if resolved is None:
            return None

        try:
            with self._lock:
                cur = self._conn.execute("SELECT * FROM diagnoses WHERE id = ?", (resolved,))
                row = cur.fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to read diagnosis history.",
                hint="Try 'autopsy history clear' if your DB is corrupted.",
            ) from exc

        if row is None:
            return None
        return dict(row)

    def _resolve_id(self, diagnosis_id: str) -> str | None:
        diagnosis_id = diagnosis_id.strip()
        if not diagnosis_id:
            return None
        if len(diagnosis_id) >= 36 and "-" in diagnosis_id:
            return diagnosis_id

        prefix = diagnosis_id
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    SELECT id, created_at, summary
                    FROM diagnoses
                    WHERE id LIKE ? || '%'
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    (prefix,),
                )
                rows = cur.fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to resolve diagnosis ID.",
                hint="Try again or use the full UUID.",
            ) from exc

        if not rows:
            return None
        if len(rows) == 1:
            return str(rows[0]["id"])

        candidates = [
            {"id": str(r["id"]), "created_at": str(r["created_at"]), "summary": str(r["summary"])}
            for r in rows
        ]
        raise HistoryAmbiguousMatchError(prefix=prefix, candidates=candidates)

    def search(self, query: str, *, limit: int = 20) -> list[DiagnosisSummaryRow]:
        """Search across summary, evidence, pr_title, commit_author (SQL LIKE)."""
        q = f"%{query.strip()}%"
        try:
            with self._lock:
                cur = self._conn.execute(
                    """
                    SELECT
                        id,
                        created_at,
                        summary,
                        category,
                        confidence,
                        commit_sha,
                        github_repo,
                        duration_s
                    FROM diagnoses
                    WHERE summary LIKE ?
                       OR evidence LIKE ?
                       OR pr_title LIKE ?
                       OR commit_author LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (q, q, q, q, int(limit)),
                )
                rows = cur.fetchall()
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to search diagnosis history.",
                hint="Try a shorter query.",
            ) from exc

        return [
            DiagnosisSummaryRow(
                id=r["id"],
                created_at=r["created_at"],
                summary=r["summary"],
                category=r["category"],
                confidence=float(r["confidence"]),
                commit_sha=r["commit_sha"],
                github_repo=r["github_repo"],
                duration_s=(float(r["duration_s"]) if r["duration_s"] is not None else None),
            )
            for r in rows
        ]

    def get_stats(self) -> dict[str, Any]:
        """Return stats for UI/CLI presentation."""
        try:
            with self._lock:
                total_row = self._conn.execute("SELECT COUNT(*) AS c FROM diagnoses").fetchone()
                total = int(total_row["c"])
            if total == 0:
                return {
                    "total": 0,
                    "date_min": None,
                    "date_max": None,
                    "avg_confidence": None,
                    "avg_duration_s": None,
                    "top_category": None,
                    "top_category_count": 0,
                    "top_repo": None,
                    "top_repo_count": 0,
                }

            with self._lock:
                date_row = self._conn.execute(
                    "SELECT MIN(created_at) AS min_dt, MAX(created_at) AS max_dt FROM diagnoses"
                ).fetchone()
                avg_row = self._conn.execute(
                    "SELECT AVG(confidence) AS avg_conf, AVG(duration_s) AS avg_dur FROM diagnoses"
                ).fetchone()
                cat_row = self._conn.execute(
                    """
                    SELECT category, COUNT(*) AS c
                    FROM diagnoses
                    GROUP BY category
                    ORDER BY c DESC
                    LIMIT 1
                    """
                ).fetchone()
                repo_row = self._conn.execute(
                    """
                    SELECT github_repo, COUNT(*) AS c
                    FROM diagnoses
                    GROUP BY github_repo
                    ORDER BY c DESC
                    LIMIT 1
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to compute history statistics.",
                hint="Try 'autopsy history clear' if your DB is corrupted.",
            ) from exc

        return {
            "total": total,
            "date_min": str(date_row["min_dt"]),
            "date_max": str(date_row["max_dt"]),
            "avg_confidence": (
                float(avg_row["avg_conf"]) if avg_row["avg_conf"] is not None else None
            ),
            "avg_duration_s": (
                float(avg_row["avg_dur"]) if avg_row["avg_dur"] is not None else None
            ),
            "top_category": (str(cat_row["category"]) if cat_row else None),
            "top_category_count": (int(cat_row["c"]) if cat_row else 0),
            "top_repo": (str(repo_row["github_repo"]) if repo_row else None),
            "top_repo_count": (int(repo_row["c"]) if repo_row else 0),
        }

    def delete(self, diagnosis_id: str) -> bool:
        """Delete a single diagnosis by ID (accepts full or prefix)."""
        resolved = self._resolve_id(diagnosis_id)
        if resolved is None:
            return False
        try:
            with self._lock:
                cur = self._conn.execute("DELETE FROM diagnoses WHERE id = ?", (resolved,))
                self._conn.commit()
                return cur.rowcount == 1
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to delete diagnosis from history.",
                hint="Try again or clear the DB if corrupted.",
            ) from exc

    def clear(self) -> int:
        """Delete all history. Returns count deleted."""
        try:
            with self._lock:
                cur = self._conn.execute("DELETE FROM diagnoses")
                self._conn.commit()
                return int(cur.rowcount)
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to clear history database.",
                hint="Check filesystem permissions.",
            ) from exc

    def export(self, path: Path, *, fmt: str = "json") -> int:
        """Export history to JSON or CSV. Returns count exported."""
        fmt = fmt.lower().strip()
        if fmt not in {"json", "csv"}:
            raise HistoryError(message=f"Unsupported export format: {fmt}", hint="Use json or csv.")

        try:
            with self._lock:
                cur = self._conn.execute("SELECT * FROM diagnoses ORDER BY created_at DESC")
                rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.Error as exc:
            raise HistoryError(
                message="Failed to export history.",
                hint="Try 'autopsy history clear' if your DB is corrupted.",
            ) from exc

        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            for r in rows:
                # Make export nicer to consume: embed parsed DiagnosisResult object.
                raw = r.get("raw_json") or "{}"
                try:
                    r["raw_json"] = json.loads(raw)
                except Exception:
                    # If corrupted, keep raw string.
                    r["raw_json"] = raw
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return len(rows)

        # CSV: flat key fields.
        fieldnames = [
            "id",
            "created_at",
            "duration_s",
            "summary",
            "category",
            "confidence",
            "commit_sha",
            "commit_author",
            "pr_title",
            "github_repo",
            "provider",
            "model",
            "prompt_version",
            "time_window",
        ]
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k) for k in fieldnames})
        return len(rows)

    def close(self) -> None:
        """Close the DB connection."""
        try:
            with self._lock:
                self._conn.close()
        except sqlite3.Error:
            # Close should never raise to callers.
            return

