from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class Database:
    """Small SQLite adapter; API/pipeline depend on this interface, not SQLite calls."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def initialize(self) -> None:
        with self.transaction() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    source_profile_id TEXT,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    change_sensitivity REAL NOT NULL,
                    stable_wait_ms INTEGER NOT NULL,
                    local_resample_fps INTEGER NOT NULL,
                    review_rounds INTEGER NOT NULL,
                    roi_mask_json TEXT,
                    applicable_features_json TEXT NOT NULL,
                    verified_video_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL NOT NULL,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    estimated_remaining_seconds INTEGER,
                    warnings_json TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    retryable INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    profile_id TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    profile_snapshot_json TEXT NOT NULL,
                    result_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    operation TEXT NOT NULL,
                    key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(operation, key)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id);
                CREATE TABLE IF NOT EXISTS pages (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    page_number INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    keyframe_ms INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    paragraphs_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    preview_key TEXT,
                    audit_json TEXT NOT NULL,
                    UNIQUE(job_id, page_number)
                );
                CREATE TABLE IF NOT EXISTS profile_proposals (
                    id TEXT PRIMARY KEY,
                    job_id TEXT UNIQUE NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    profile_id TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    agreement INTEGER NOT NULL,
                    review_rounds INTEGER NOT NULL,
                    estimated_impact TEXT NOT NULL,
                    applied_profile_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            now = utcnow()
            conn.execute(
                """
                INSERT OR IGNORE INTO profiles (
                    id,name,version,source_profile_id,is_default,change_sensitivity,
                    stable_wait_ms,local_resample_fps,review_rounds,roi_mask_json,
                    applicable_features_json,verified_video_count,last_used_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "profile_smart_default",
                    "智能默认",
                    1,
                    None,
                    1,
                    0.55,
                    900,
                    10,
                    3,
                    None,
                    json.dumps(["progressive_slides", "mixed_language"]),
                    0,
                    None,
                    now,
                    now,
                ),
            )

    @staticmethod
    def profile_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "source_profile_id": row["source_profile_id"],
            "is_default": bool(row["is_default"]),
            "change_sensitivity": row["change_sensitivity"],
            "stable_wait_ms": row["stable_wait_ms"],
            "local_resample_fps": row["local_resample_fps"],
            "review_rounds": row["review_rounds"],
            "roi_mask": _loads(row["roi_mask_json"], None),
            "applicable_features": _loads(row["applicable_features_json"], []),
            "verified_video_count": row["verified_video_count"],
            "last_used_at": row["last_used_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_profiles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM profiles ORDER BY is_default DESC, created_at, id"
            ).fetchall()
        return [self.profile_dict(row) for row in rows]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        return self.profile_dict(row) if row else None

    def create_profile(self, values: dict[str, Any], *, profile_id: str | None = None) -> dict[str, Any]:
        profile_id = profile_id or f"profile_{uuid.uuid4().hex}"
        now = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO profiles (
                    id,name,version,source_profile_id,is_default,change_sensitivity,
                    stable_wait_ms,local_resample_fps,review_rounds,roi_mask_json,
                    applicable_features_json,verified_video_count,last_used_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    profile_id,
                    values["name"],
                    1,
                    values.get("source_profile_id"),
                    0,
                    values.get("change_sensitivity", 0.55),
                    values.get("stable_wait_ms", 900),
                    values.get("local_resample_fps", 10),
                    values.get("review_rounds", 3),
                    json.dumps(values.get("roi_mask")),
                    json.dumps(values.get("applicable_features", [])),
                    0,
                    None,
                    now,
                    now,
                ),
            )
        return self.get_profile(profile_id)  # type: ignore[return-value]

    def update_profile(
        self, profile_id: str, expected_version: int, changes: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if not row:
                return "missing", None
            if row["is_default"]:
                return "default", self.profile_dict(row)
            if row["version"] != expected_version:
                return "conflict", self.profile_dict(row)
            allowed = {
                "name": "name",
                "change_sensitivity": "change_sensitivity",
                "stable_wait_ms": "stable_wait_ms",
                "local_resample_fps": "local_resample_fps",
                "review_rounds": "review_rounds",
                "roi_mask": "roi_mask_json",
                "applicable_features": "applicable_features_json",
            }
            assignments: list[str] = []
            params: list[Any] = []
            for field, value in changes.items():
                if field not in allowed:
                    continue
                assignments.append(f"{allowed[field]}=?")
                params.append(json.dumps(value) if field in {"roi_mask", "applicable_features"} else value)
            assignments.extend(["version=version+1", "updated_at=?"])
            params.extend([utcnow(), profile_id])
            conn.execute(f"UPDATE profiles SET {', '.join(assignments)} WHERE id=?", params)
        return "updated", self.get_profile(profile_id)

    def delete_profile(self, profile_id: str) -> str:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT is_default FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if not row:
                return "missing"
            if row["is_default"]:
                return "default"
            conn.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        return "deleted"

    def idempotency_lookup(self, operation: str, key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM idempotency_keys WHERE operation=? AND key=?",
                (operation, key),
            ).fetchone()

    def create_job(
        self,
        *,
        job_id: str,
        request_json: str,
        request_hash: str,
        token_hash: str,
        profile: dict[str, Any],
        expires_at: str,
        idempotency_key: str,
    ) -> tuple[str, str]:
        now = utcnow()
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency_keys WHERE operation='create_job' AND key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    return existing["resource_id"], "conflict"
                return existing["resource_id"], "replay"
            conn.execute(
                """
                INSERT INTO jobs (
                    id,request_json,request_hash,token_hash,status,stage,progress,
                    warnings_json,profile_id,profile_version,profile_snapshot_json,
                    created_at,updated_at,expires_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    request_json,
                    request_hash,
                    token_hash,
                    "queued",
                    "queued",
                    0.0,
                    "[]",
                    profile["id"],
                    profile["version"],
                    json.dumps(profile, ensure_ascii=False),
                    now,
                    now,
                    expires_at,
                ),
            )
            conn.execute(
                "INSERT INTO idempotency_keys VALUES (?,?,?,?,?)",
                ("create_job", idempotency_key, request_hash, job_id, now),
            )
        return job_id, "created"

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "stage",
            "progress",
            "page_count",
            "estimated_remaining_seconds",
            "warnings_json",
            "error_code",
            "error_message",
            "retryable",
            "cancel_requested",
            "attempt",
            "result_key",
        }
        values = [(key, value) for key, value in changes.items() if key in allowed]
        if not values:
            return
        assignments = [f"{key}=?" for key, _ in values] + ["updated_at=?"]
        params = [value for _, value in values] + [utcnow(), job_id]
        with self.transaction() as conn:
            conn.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE id=?", params)

    def add_event(self, job_id: str, event_type: str, data: dict[str, Any]) -> int:
        now = utcnow()
        with self.transaction() as conn:
            job = conn.execute("SELECT attempt FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempt = int(job["attempt"]) if job is not None else None
            payload = {
                "job_id": job_id,
                "at": now,
                **({"attempt": attempt} if attempt is not None else {}),
                **data,
            }
            cursor = conn.execute(
                "INSERT INTO events(job_id,event_type,data_json,created_at) VALUES (?,?,?,?)",
                (job_id, event_type, json.dumps(payload, ensure_ascii=False), now),
            )
            return int(cursor.lastrowid)

    def list_events(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE job_id=? AND id>? ORDER BY id",
                (job_id, after_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "data": _loads(row["data_json"], {}),
            }
            for row in rows
        ]

    def replace_pages(self, job_id: str, pages: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM pages WHERE job_id=?", (job_id,))
            for page in pages:
                conn.execute(
                    """
                    INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"page_{job_id}_{page['page_number']}",
                        job_id,
                        page["page_number"],
                        page["start_ms"],
                        page["end_ms"],
                        page["keyframe_ms"],
                        page["title"],
                        json.dumps(page["paragraphs"], ensure_ascii=False),
                        page["confidence"],
                        page.get("preview_key"),
                        json.dumps(page.get("audit", {}), ensure_ascii=False),
                    ),
                )

    def list_pages(self, job_id: str, offset: int, limit: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pages WHERE job_id=? ORDER BY page_number LIMIT ? OFFSET ?",
                (job_id, limit, offset),
            ).fetchall()
        return [
            {
                "page_number": row["page_number"],
                "start_ms": row["start_ms"],
                "end_ms": row["end_ms"],
                "keyframe_ms": row["keyframe_ms"],
                "title": row["title"],
                "paragraphs": _loads(row["paragraphs_json"], []),
                "confidence": row["confidence"],
                "preview_key": row["preview_key"],
            }
            for row in rows
        ]

    def create_proposal(self, job: dict[str, Any], review_rounds: int) -> dict[str, Any]:
        proposal_id = f"proposal_{uuid.uuid4().hex}"
        snapshot = _loads(job.get("profile_snapshot_json"), {})
        old_wait = int(snapshot.get("stable_wait_ms", 900))
        new_wait = min(10_000, old_wait + 150)
        changes = [{"field": "stable_wait_ms", "old_value": old_wait, "new_value": new_wait}]
        now = utcnow()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO profile_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    proposal_id,
                    job["id"],
                    job["profile_id"],
                    job["profile_version"],
                    "pending",
                    json.dumps(changes),
                    "progressive_slides_with_slow_reveals",
                    json.dumps(["algorithm_and_vlm_agreed", "stable_across_two_rounds"]),
                    1,
                    review_rounds,
                    "Fewer partial-load frame selections on similar videos",
                    None,
                    now,
                    now,
                ),
            )
        return self.get_proposal_by_job(job["id"])  # type: ignore[return-value]

    @staticmethod
    def proposal_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "profile_id": row["profile_id"],
            "profile_version": row["profile_version"],
            "status": row["status"],
            "changes": _loads(row["changes_json"], []),
            "scope": row["scope"],
            "evidence": _loads(row["evidence_json"], []),
            "algorithm_vlm_agreement": bool(row["agreement"]),
            "review_rounds": row["review_rounds"],
            "estimated_impact": row["estimated_impact"],
            "applied_profile_id": row["applied_profile_id"],
        }

    def get_proposal_by_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM profile_proposals WHERE job_id=?", (job_id,)
            ).fetchone()
        return self.proposal_dict(row) if row else None

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM profile_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        return self.proposal_dict(row) if row else None

    def mark_proposal_applied(self, proposal_id: str, profile_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE profile_proposals SET status='applied',applied_profile_id=?,updated_at=? WHERE id=?",
                (profile_id, utcnow(), proposal_id),
            )

    def store_idempotency(
        self, operation: str, key: str, request_hash: str, resource_id: str
    ) -> bool:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency_keys WHERE operation=? AND key=?",
                (operation, key),
            ).fetchone()
            if existing:
                return existing["request_hash"] == request_hash
            conn.execute(
                "INSERT INTO idempotency_keys VALUES (?,?,?,?,?)",
                (operation, key, request_hash, resource_id, utcnow()),
            )
        return True
