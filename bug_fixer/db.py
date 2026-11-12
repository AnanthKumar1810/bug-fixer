"""SQLite database and artifact repository for persistent run evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bug_fixer.models import EventRecord, ManifestEntry, RunManifest, RunRecord, RunState


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class EvidenceStore:
    def __init__(self, db_path: Path, artifacts_dir: Path) -> None:
        self.db_path = db_path.resolve()
        self.artifacts_dir = artifacts_dir.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    issue_url TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT,
                    repo_owner TEXT NOT NULL,
                    repo_name TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reproducer_nodeid TEXT NOT NULL,
                    reproducer_hash TEXT NOT NULL,
                    candidate_patch_hash TEXT,
                    retry_budget INTEGER NOT NULL DEFAULT 2,
                    attempts_used INTEGER NOT NULL DEFAULT 0,
                    workspace_hash TEXT,
                    provenance TEXT NOT NULL DEFAULT 'fixture',
                    base_branch TEXT NOT NULL DEFAULT 'main',
                    reviewer TEXT,
                    reviewer_status TEXT,
                    branch_name TEXT,
                    pr_url TEXT,
                    branch_protection TEXT,
                    publication_mode TEXT,
                    manifest_hash TEXT,
                    error_detail TEXT,
                    challenge_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    inputs_ref TEXT,
                    outputs_ref TEXT,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_hash TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, attempt_number);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
            """)
            # Auto-migrate any missing columns for pre-existing databases
            cursor = conn.execute("PRAGMA table_info(runs);")
            columns = {row["name"] for row in cursor.fetchall()}
            for col, col_type in [
                ("issue_title", "TEXT"),
                ("provenance", "TEXT DEFAULT 'fixture'"),
                ("origin_provenance", "TEXT DEFAULT 'fixture'"),
                ("base_branch", "TEXT DEFAULT 'main'"),
                ("reviewer", "TEXT"),
                ("reviewer_status", "TEXT"),
                ("branch_name", "TEXT"),
                ("pr_number", "INTEGER"),
                ("pr_url", "TEXT"),
                ("branch_protection", "TEXT"),
                ("publication_mode", "TEXT"),
                ("verified_tree_id", "TEXT"),
                ("publication_commit_sha", "TEXT"),
                ("manifest_hash", "TEXT"),
                ("challenge_status", "TEXT"),
            ]:
                if col not in columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type};")

    def save_run(self, run: RunRecord, workspace_hash: str | None = None) -> None:
        now = _utc_now()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO runs (
                    run_id, issue_url, issue_number, issue_title, repo_owner, repo_name,
                    base_sha, stage, status, reproducer_nodeid, reproducer_hash,
                    candidate_patch_hash, retry_budget, attempts_used, workspace_hash,
                    provenance, origin_provenance, base_branch, reviewer, reviewer_status, branch_name,
                    pr_number, pr_url, branch_protection, publication_mode,
                    verified_tree_id, publication_commit_sha, manifest_hash,
                    error_detail, challenge_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    issue_title = COALESCE(excluded.issue_title, runs.issue_title),
                    base_sha = CASE WHEN excluded.base_sha != '' THEN excluded.base_sha ELSE runs.base_sha END,
                    stage = excluded.stage,
                    status = excluded.status,
                    reproducer_nodeid = CASE WHEN excluded.reproducer_nodeid != '' THEN excluded.reproducer_nodeid ELSE runs.reproducer_nodeid END,
                    reproducer_hash = CASE WHEN excluded.reproducer_hash != '' THEN excluded.reproducer_hash ELSE runs.reproducer_hash END,
                    candidate_patch_hash = excluded.candidate_patch_hash,
                    retry_budget = excluded.retry_budget,
                    attempts_used = excluded.attempts_used,
                    workspace_hash = COALESCE(excluded.workspace_hash, runs.workspace_hash),
                    provenance = COALESCE(excluded.provenance, runs.provenance),
                    origin_provenance = COALESCE(runs.origin_provenance, excluded.origin_provenance),
                    base_branch = COALESCE(excluded.base_branch, runs.base_branch),
                    reviewer = COALESCE(excluded.reviewer, runs.reviewer),
                    reviewer_status = COALESCE(excluded.reviewer_status, runs.reviewer_status),
                    branch_name = COALESCE(excluded.branch_name, runs.branch_name),
                    pr_number = COALESCE(excluded.pr_number, runs.pr_number),
                    pr_url = COALESCE(excluded.pr_url, runs.pr_url),
                    branch_protection = COALESCE(excluded.branch_protection, runs.branch_protection),
                    publication_mode = COALESCE(excluded.publication_mode, runs.publication_mode),
                    verified_tree_id = COALESCE(excluded.verified_tree_id, runs.verified_tree_id),
                    publication_commit_sha = COALESCE(excluded.publication_commit_sha, runs.publication_commit_sha),
                    manifest_hash = COALESCE(excluded.manifest_hash, runs.manifest_hash),
                    error_detail = excluded.error_detail,
                    challenge_status = COALESCE(excluded.challenge_status, runs.challenge_status),
                    updated_at = excluded.updated_at
            """, (
                run.run_id, run.issue_url, run.issue_number, getattr(run, "issue_title", None), run.repo_owner, run.repo_name,
                run.base_sha, run.stage.value if hasattr(run.stage, "value") else str(run.stage),
                run.status, run.reproducer_nodeid, run.reproducer_hash,
                run.candidate_patch_hash, run.retry_budget, run.attempts_used, workspace_hash,
                run.provenance, getattr(run, "origin_provenance", run.provenance or "fixture"),
                run.base_branch, run.reviewer, run.reviewer_status, run.branch_name,
                run.pr_number, run.pr_url, run.branch_protection, run.publication_mode,
                run.verified_tree_id, run.publication_commit_sha, run.manifest_hash,
                run.error_detail, getattr(run, "challenge_status", None), run.created_at or now, now
            ))

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            keys = row.keys()
            return RunRecord(
                run_id=row["run_id"],
                issue_url=row["issue_url"],
                issue_number=row["issue_number"],
                issue_title=row["issue_title"] if "issue_title" in keys else None,
                repo_owner=row["repo_owner"],
                repo_name=row["repo_name"],
                base_sha=row["base_sha"],
                stage=RunState(row["stage"]),
                status=row["status"],
                reproducer_nodeid=row["reproducer_nodeid"],
                reproducer_hash=row["reproducer_hash"],
                candidate_patch_hash=row["candidate_patch_hash"],
                retry_budget=row["retry_budget"],
                attempts_used=row["attempts_used"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                provenance=row["provenance"] if "provenance" in keys and row["provenance"] else "fixture",
                origin_provenance=row["origin_provenance"] if "origin_provenance" in keys and row["origin_provenance"] else (row["provenance"] if "provenance" in keys and row["provenance"] else "fixture"),
                base_branch=row["base_branch"] if "base_branch" in keys and row["base_branch"] else "main",
                reviewer=row["reviewer"],
                reviewer_status=row["reviewer_status"] if "reviewer_status" in keys else None,
                branch_name=row["branch_name"],
                pr_number=row["pr_number"] if "pr_number" in keys else None,
                pr_url=row["pr_url"],
                branch_protection=row["branch_protection"] if "branch_protection" in keys else None,
                publication_mode=row["publication_mode"] if "publication_mode" in keys else None,
                verified_tree_id=row["verified_tree_id"] if "verified_tree_id" in keys else None,
                publication_commit_sha=row["publication_commit_sha"] if "publication_commit_sha" in keys else None,
                manifest_hash=row["manifest_hash"] if "manifest_hash" in keys else None,
                error_detail=row["error_detail"],
                challenge_status=row["challenge_status"] if "challenge_status" in keys else None,
            )

    def get_workspace_hash(self, run_id: str) -> str | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT workspace_hash FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return row["workspace_hash"] if row else None

    def record_event(
        self,
        run_id: str,
        stage: str,
        event_type: str,
        *,
        attempt_number: int = 0,
        inputs_ref: str | None = None,
        outputs_ref: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> EventRecord:
        event_id = "evt_" + uuid.uuid4().hex[:12]
        ts = _utc_now()
        payload = data or {}
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO events (
                    event_id, run_id, attempt_number, stage, timestamp,
                    event_type, inputs_ref, outputs_ref, data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event_id, run_id, attempt_number, stage, ts,
                event_type, inputs_ref, outputs_ref, json.dumps(payload, default=str)
            ))
        return EventRecord(
            event_id=event_id,
            run_id=run_id,
            attempt_number=attempt_number,
            stage=stage,
            timestamp=ts,
            event_type=event_type,
            inputs_ref=inputs_ref,
            outputs_ref=outputs_ref,
            data=payload,
        )

    def list_events(self, run_id: str) -> list[EventRecord]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY timestamp ASC, rowid ASC",
                (run_id,)
            ).fetchall()
            return [
                EventRecord(
                    event_id=r["event_id"],
                    run_id=r["run_id"],
                    attempt_number=r["attempt_number"],
                    stage=r["stage"],
                    timestamp=r["timestamp"],
                    event_type=r["event_type"],
                    inputs_ref=r["inputs_ref"],
                    outputs_ref=r["outputs_ref"],
                    data=json.loads(r["data_json"]),
                )
                for r in rows
            ]

    def store_artifact(
        self,
        run_id: str,
        artifact_type: str,
        content: str | bytes,
        filename: str,
    ) -> tuple[str, Path]:
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        art_hash = compute_sha256(raw_bytes)
        run_art_dir = self.artifacts_dir / run_id
        run_art_dir.mkdir(parents=True, exist_ok=True)
        file_path = run_art_dir / filename
        file_path.write_bytes(raw_bytes)

        now = _utc_now()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO artifacts (
                    artifact_hash, run_id, artifact_type, file_path, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_hash) DO UPDATE SET
                    file_path = excluded.file_path,
                    created_at = excluded.created_at
            """, (
                art_hash, run_id, artifact_type, str(file_path), len(raw_bytes), now
            ))
        return art_hash, file_path

    def get_artifact(self, artifact_hash: str) -> bytes | None:
        with self._get_conn() as conn:
            row = conn.execute("SELECT file_path FROM artifacts WHERE artifact_hash = ?", (artifact_hash,)).fetchone()
            if not row:
                return None
            p = Path(row["file_path"])
            if p.is_file():
                return p.read_bytes()
            return None

    def store_manifest(self, run_id: str, manifest: RunManifest) -> tuple[str, Path]:
        """Store run manifest only after referenced files exist, and update run record."""
        manifest_json = json.dumps(manifest.model_dump(), indent=2)
        art_hash, path = self.store_artifact(run_id, "manifest", manifest_json, "manifest.json")
        with self._get_conn() as conn:
            conn.execute("UPDATE runs SET manifest_hash = ? WHERE run_id = ?", (art_hash, run_id))
        return art_hash, path

    def get_manifest(self, run_id: str) -> RunManifest | None:
        """Load manifest for a run if present."""
        manifest_path = self.artifacts_dir / run_id / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            return RunManifest(**data)
        except Exception:
            return None

    def verify_manifest(self, run_id: str) -> tuple[bool, list[str]]:
        """Verify that all files in manifest exist on disk and hashes match."""
        manifest = self.get_manifest(run_id)
        if manifest is None:
            return False, ["Manifest file manifest.json does not exist or is corrupted."]

        errors: list[str] = []
        run_art_dir = self.artifacts_dir / run_id
        for rel_name, entry in manifest.files.items():
            fpath = run_art_dir / rel_name
            if not fpath.is_file():
                errors.append(f"Required artifact file missing: {rel_name}")
                continue
            raw_bytes = fpath.read_bytes()
            actual_hash = hashlib.sha256(raw_bytes).hexdigest()
            # Also check LF-normalized hash for cross-platform tolerance (Windows CRLF)
            if actual_hash != entry.sha256:
                norm_hash = hashlib.sha256(raw_bytes.replace(b"\r\n", b"\n")).hexdigest()
                if norm_hash != entry.sha256:
                    errors.append(
                        f"Artifact hash mismatch for {rel_name}: expected {entry.sha256[:12]}..., got {actual_hash[:12]}..."
                    )
        return len(errors) == 0, errors

    def get_events(self, run_id: str) -> list[EventRecord]:
        """Alias for list_events."""
        return self.list_events(run_id)

    def get_artifacts_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """List all artifact records recorded for a run."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT artifact_hash, artifact_type, file_path, byte_size, created_at FROM artifacts WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,)
            ).fetchall()
            return [dict(r) for r in rows]
