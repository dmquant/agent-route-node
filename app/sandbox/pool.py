"""Sandbox Pool — provision, track, and destroy workspaces.

Workspaces are cattle: provision() creates them, destroy() removes them.
Every sandbox gets a unique ID, TTL, and metadata for tracking.
"""

import os
import shutil
import time
import uuid
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional, List, Dict

from app.config import get_data_dir, get_db_path

# Workspace root
_BASE_DIR = os.path.join(get_data_dir(), 'workspaces')

# DB path
_DB_PATH = get_db_path()


@dataclass
class SandboxInfo:
    """Metadata for a provisioned sandbox workspace."""
    id: str
    session_id: Optional[str]
    path: str
    status: str  # "active" | "idle" | "destroyed"
    created_at: int
    last_used_at: int
    ttl_seconds: int = 86400  # Default 24h TTL
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "path": self.path,
            "status": self.status,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "ttl_seconds": self.ttl_seconds,
            "metadata": self.metadata,
            "expired": self.is_expired,
        }

    @property
    def is_expired(self) -> bool:
        return (time.time() * 1000 - self.last_used_at) > (self.ttl_seconds * 1000)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_sandbox_tables():
    """Create the sandboxes table."""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sandboxes (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                ttl_seconds INTEGER NOT NULL DEFAULT 86400,
                metadata TEXT DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_sandboxes_session
                ON sandboxes(session_id);
            CREATE INDEX IF NOT EXISTS idx_sandboxes_status
                ON sandboxes(status);
        """)
        conn.commit()
        print("[SandboxPool] Tables initialized.")
    finally:
        conn.close()


class SandboxPool:
    """Provision, track, and destroy workspace sandboxes.

    provision() → Create isolated workspace directory + DB record
    touch()     → Mark a sandbox as recently used (reset TTL)
    destroy()   → Remove workspace directory + mark destroyed
    gc()        → Garbage-collect expired sandboxes
    list()      → List all active sandboxes
    """

    def __init__(self, base_dir: str = _BASE_DIR, max_active: int = 50):
        self.base_dir = base_dir
        self.max_active = max_active
        os.makedirs(base_dir, exist_ok=True)

    def provision(
        self,
        session_id: Optional[str] = None,
        name: Optional[str] = None,
        ttl_seconds: int = 86400,
        metadata: Optional[dict] = None,
    ) -> SandboxInfo:
        """Provision a new sandbox workspace.

        Creates a directory under workspaces/ and records it in the DB.
        """
        sandbox_id = uuid.uuid4().hex[:12]
        dir_name = f"{name or 'sandbox'}_{sandbox_id}"
        sandbox_path = os.path.join(self.base_dir, dir_name)
        os.makedirs(sandbox_path, exist_ok=True)

        ts = int(time.time() * 1000)
        meta_json = json.dumps(metadata or {})

        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO sandboxes
                   (id, session_id, path, status, created_at, last_used_at, ttl_seconds, metadata)
                   VALUES (?, ?, ?, 'active', ?, ?, ?, ?)""",
                (sandbox_id, session_id, sandbox_path, ts, ts, ttl_seconds, meta_json),
            )
            conn.commit()
        finally:
            conn.close()

        print(f"[SandboxPool] Provisioned: {sandbox_id} → {sandbox_path}")

        return SandboxInfo(
            id=sandbox_id,
            session_id=session_id,
            path=sandbox_path,
            status="active",
            created_at=ts,
            last_used_at=ts,
            ttl_seconds=ttl_seconds,
            metadata=metadata or {},
        )

    def touch(self, sandbox_id: str) -> bool:
        """Mark a sandbox as recently used (reset TTL timer)."""
        ts = int(time.time() * 1000)
        conn = _get_conn()
        try:
            result = conn.execute(
                "UPDATE sandboxes SET last_used_at = ?, status = 'active' WHERE id = ?",
                (ts, sandbox_id),
            )
            conn.commit()
            return result.rowcount > 0
        finally:
            conn.close()

    def get(self, sandbox_id: str) -> Optional[SandboxInfo]:
        """Get sandbox info by ID."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)
            ).fetchone()
            if not row:
                return None
            return self._from_row(dict(row))
        finally:
            conn.close()

    def get_for_session(self, session_id: str) -> Optional[SandboxInfo]:
        """Get active sandbox for a session."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM sandboxes WHERE session_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return self._from_row(dict(row))
        finally:
            conn.close()

    def destroy(self, sandbox_id: str) -> bool:
        """Destroy a sandbox — remove directory and mark destroyed."""
        sandbox = self.get(sandbox_id)
        if not sandbox:
            return False

        # Remove directory if it exists
        if os.path.exists(sandbox.path):
            try:
                shutil.rmtree(sandbox.path)
                print(f"[SandboxPool] Destroyed directory: {sandbox.path}")
            except OSError as e:
                print(f"[SandboxPool] Failed to remove {sandbox.path}: {e}")

        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE sandboxes SET status = 'destroyed' WHERE id = ?",
                (sandbox_id,),
            )
            conn.commit()
        finally:
            conn.close()

        return True

    def gc(self) -> List[str]:
        """Garbage-collect expired sandboxes."""
        conn = _get_conn()
        destroyed = []
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE status = 'active'"
            ).fetchall()

            for row in rows:
                info = self._from_row(dict(row))
                if info.is_expired:
                    if self.destroy(info.id):
                        destroyed.append(info.id)

        finally:
            conn.close()

        if destroyed:
            print(f"[SandboxPool] GC: destroyed {len(destroyed)} expired sandboxes")

        return destroyed

    def list_active(self) -> List[SandboxInfo]:
        """List all active (non-destroyed) sandboxes."""
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE status != 'destroyed' ORDER BY last_used_at DESC"
            ).fetchall()
            return [self._from_row(dict(r)) for r in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict:
        """Pool utilization statistics."""
        conn = _get_conn()
        try:
            active = conn.execute(
                "SELECT COUNT(*) as cnt FROM sandboxes WHERE status = 'active'"
            ).fetchone()["cnt"]
            total = conn.execute(
                "SELECT COUNT(*) as cnt FROM sandboxes"
            ).fetchone()["cnt"]
            destroyed = conn.execute(
                "SELECT COUNT(*) as cnt FROM sandboxes WHERE status = 'destroyed'"
            ).fetchone()["cnt"]

            return {
                "active": active,
                "total": total,
                "destroyed": destroyed,
                "max_active": self.max_active,
                "utilization": round(active / self.max_active, 3) if self.max_active > 0 else 0,
                "base_dir": self.base_dir,
            }
        finally:
            conn.close()

    def _from_row(self, row: dict) -> SandboxInfo:
        meta = row.get("metadata", "{}")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        return SandboxInfo(
            id=row["id"],
            session_id=row.get("session_id"),
            path=row["path"],
            status=row["status"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            ttl_seconds=row.get("ttl_seconds", 86400),
            metadata=meta,
        )


# ─── Global Singleton ──────────────────────
sandbox_pool = SandboxPool()
