"""Session Event Manager — durable session state.

Key interfaces (from Anthropic Managed Agents):
  emitEvent(session_id, event)  — append to log
  getEvents(session_id, range)  — interrogate context
  wake(session_id)              — resume after brain crash
  checkpoint(session_id)        — save context snapshot

The session survives brain crashes: wake() + getEvents() = recovery.
"""

import os
import json
import time
import sqlite3
import uuid
from typing import Optional, List

from app.session.events import EventType, SessionEvent

from app.config import get_db_path

# ─── Database path (co-located with session store) ───────
_DB_PATH = get_db_path()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def init_event_tables():
    """Create the session_events and session_checkpoints tables.

    Safe to call multiple times — uses IF NOT EXISTS.
    """
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                agent TEXT,
                content TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                timestamp INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_session
                ON session_events(session_id);
            CREATE INDEX IF NOT EXISTS idx_events_type
                ON session_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_ts
                ON session_events(timestamp);

            CREATE TABLE IF NOT EXISTS session_checkpoints (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                event_position INTEGER NOT NULL,
                context_summary TEXT DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                ON session_checkpoints(session_id);
        """)
        conn.commit()
        print("[SessionEvents] Event tables initialized.")
    finally:
        conn.close()


class SessionEventManager:
    """Durable session event log — the source of truth.

    emitEvent()  → append immutable event
    getEvents()  → interrogate log with positional slicing
    wake()       → resume session after brain crash
    checkpoint() → save context snapshot for recovery
    """

    def emit_event(
        self,
        session_id: str,
        event_type: EventType,
        content: str = "",
        agent: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[SessionEvent]:
        """Append an immutable event to the session log. Returns None on DB error."""
        ts = int(time.time() * 1000)
        meta_json = json.dumps(metadata or {})

        try:
            conn = _get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO session_events
                       (session_id, event_type, agent, content, metadata, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (session_id, event_type.value if isinstance(event_type, EventType) else event_type,
                     agent, content, meta_json, ts),
                )
                conn.commit()
                event_id = cur.lastrowid
            finally:
                conn.close()
        except Exception:
            return None

        return SessionEvent(
            id=event_id,
            session_id=session_id,
            event_type=event_type,
            agent=agent,
            content=content,
            metadata=metadata or {},
            timestamp=ts,
        )

    def get_events(
        self,
        session_id: str,
        start: int = 0,
        end: int = -1,
        event_types: Optional[List[EventType]] = None,
        limit: int = 500,
    ) -> List[SessionEvent]:
        """Interrogate the event log with positional slicing.

        This is the key Anthropic insight: context is a programmable
        object that lives outside the context window, enabling:
        - Rewinding to see lead-up before a specific event
        - Picking up from where the brain last stopped reading
        - Filtering by event type (only tool results, only errors, etc.)
        """
        conn = _get_conn()
        try:
            query = "SELECT * FROM session_events WHERE session_id = ?"
            params: list = [session_id]

            if start > 0:
                query += " AND id >= ?"
                params.append(start)

            if end > 0:
                query += " AND id <= ?"
                params.append(end)

            if event_types:
                placeholders = ",".join("?" for _ in event_types)
                query += f" AND event_type IN ({placeholders})"
                params.extend(
                    et.value if isinstance(et, EventType) else et
                    for et in event_types
                )

            query += " ORDER BY id ASC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [SessionEvent.from_row(dict(r)) for r in rows]
        finally:
            conn.close()

    def get_event_count(self, session_id: str) -> int:
        """Get total number of events for a session."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM session_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row["cnt"] if row else 0
        finally:
            conn.close()

    def get_latest_event(self, session_id: str) -> Optional[SessionEvent]:
        """Get the most recent event for a session."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM session_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return SessionEvent.from_row(dict(row)) if row else None
        finally:
            conn.close()

    def wake(self, session_id: str) -> dict:
        """Resume a session after brain crash.

        Returns session metadata + last event position for context rebuild.
        """
        self.emit_event(session_id, EventType.SESSION_RESUMED)
        count = self.get_event_count(session_id)
        latest = self.get_latest_event(session_id)

        return {
            "session_id": session_id,
            "event_count": count,
            "last_event": latest.to_dict() if latest else None,
            "status": "resumed",
        }

    def checkpoint(self, session_id: str, summary: str = "") -> str:
        """Save a context checkpoint for crash recovery.

        Returns checkpoint ID.
        """
        checkpoint_id = uuid.uuid4().hex[:16]
        ts = int(time.time() * 1000)

        # Get current event position
        latest = self.get_latest_event(session_id)
        event_pos = latest.id if latest else 0

        conn = _get_conn()
        try:
            conn.execute(
                """INSERT INTO session_checkpoints
                   (id, session_id, event_position, context_summary, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (checkpoint_id, session_id, event_pos, summary, ts),
            )
            conn.commit()
        finally:
            conn.close()

        # Also emit a checkpoint event in the log
        self.emit_event(
            session_id,
            EventType.CONTEXT_CHECKPOINT,
            content=f"Checkpoint {checkpoint_id}",
            metadata={"checkpoint_id": checkpoint_id, "event_position": event_pos},
        )

        return checkpoint_id

    def get_checkpoint(self, checkpoint_id: str) -> Optional[dict]:
        """Retrieve a checkpoint by ID."""
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM session_checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_token_usage(self, session_id: str) -> dict:
        """Aggregate token metrics from METRIC events."""
        events = self.get_events(
            session_id, event_types=[EventType.METRIC]
        )
        total_input = 0
        total_output = 0
        for e in events:
            total_input += e.metadata.get("input_tokens", 0)
            total_output += e.metadata.get("output_tokens", 0)

        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "total_tokens": total_input + total_output,
            "metric_events": len(events),
        }

    def get_session_summary(self, session_id: str) -> dict:
        """Get a high-level summary of a session's event log."""
        conn = _get_conn()
        try:
            # Count by event type
            rows = conn.execute(
                """SELECT event_type, COUNT(*) as cnt
                   FROM session_events WHERE session_id = ?
                   GROUP BY event_type""",
                (session_id,),
            ).fetchall()

            type_counts = {r["event_type"]: r["cnt"] for r in rows}

            # First and last event timestamps
            first = conn.execute(
                "SELECT timestamp FROM session_events WHERE session_id = ? ORDER BY id ASC LIMIT 1",
                (session_id,),
            ).fetchone()

            last = conn.execute(
                "SELECT timestamp FROM session_events WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()

            return {
                "session_id": session_id,
                "total_events": sum(type_counts.values()),
                "event_types": type_counts,
                "first_event_ts": first["timestamp"] if first else None,
                "last_event_ts": last["timestamp"] if last else None,
                "duration_ms": (last["timestamp"] - first["timestamp"]) if first and last else 0,
            }
        finally:
            conn.close()

    def get_recent_events(self, limit: int = 100) -> list:
        """Get most recent events across all sessions (replaces legacy get_logs)."""
        conn = _get_conn()
        try:
            rows = conn.execute(
                """SELECT id, session_id, event_type, agent, content, metadata, timestamp
                   FROM session_events ORDER BY id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "event_type": r["event_type"],
                    "agent": r["agent"],
                    "content": (r["content"] or "")[:200],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
        finally:
            conn.close()


# ─── Global Singleton ──────────────────────
session_events = SessionEventManager()
