"""
Report Store — SQLite persistence for AI-generated daily reports.

Schema:
    reports(
        id          TEXT PRIMARY KEY,     -- UUID
        date        TEXT NOT NULL,        -- The report date, e.g. '2026-04-10'
        days        INTEGER DEFAULT 1,    -- Period span
        agent       TEXT NOT NULL,        -- Agent used for generation
        content     TEXT NOT NULL,        -- Markdown report body
        stats_json  TEXT,                 -- JSON snapshot of stats at generation time
        prompt      TEXT,                 -- The prompt sent to the agent
        created_at  INTEGER NOT NULL,     -- Epoch ms
        title       TEXT,                 -- Optional human title
        pinned      INTEGER DEFAULT 0     -- 1 = pinned/starred
    )
"""

import sqlite3
import os
import json
import uuid
import time
from typing import Dict, List, Any, Optional

from app.config import get_db_path
DB_PATH = get_db_path()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_report_tables():
    """Create the reports table if it doesn't exist."""
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id          TEXT PRIMARY KEY,
            date        TEXT NOT NULL,
            days        INTEGER DEFAULT 1,
            agent       TEXT NOT NULL,
            content     TEXT NOT NULL,
            stats_json  TEXT,
            prompt      TEXT,
            created_at  INTEGER NOT NULL,
            title       TEXT,
            pinned      INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC)
    """)
    conn.commit()
    conn.close()
    print("[ReportStore] Tables initialized.")


def save_report(
    date: str,
    days: int,
    agent: str,
    content: str,
    stats: Optional[Dict] = None,
    prompt: Optional[str] = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Save a generated report and return it as a dict."""
    report_id = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)

    # Auto-generate title from first heading or date
    if not title:
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('# '):
                title = line[2:].strip()
                break
        if not title:
            title = f"Daily Report — {date}"

    conn = _get_conn()
    conn.execute(
        """INSERT INTO reports (id, date, days, agent, content, stats_json, prompt, created_at, title, pinned)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
        (
            report_id,
            date,
            days,
            agent,
            content,
            json.dumps(stats) if stats else None,
            prompt,
            now_ms,
            title,
        ),
    )
    conn.commit()
    conn.close()

    return {
        "id": report_id,
        "date": date,
        "days": days,
        "agent": agent,
        "content": content,
        "stats_json": stats,
        "prompt": prompt,
        "created_at": now_ms,
        "title": title,
        "pinned": False,
    }


def list_reports(
    limit: int = 50,
    date: Optional[str] = None,
    agent: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List reports, optionally filtered by date or agent. Returns metadata only (no full content)."""
    conn = _get_conn()

    where_clauses = []
    params: list = []

    if date:
        where_clauses.append("date = ?")
        params.append(date)
    if agent:
        where_clauses.append("agent = ?")
        params.append(agent)

    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params.append(limit)

    rows = conn.execute(
        f"""SELECT id, date, days, agent, created_at, title, pinned,
                   LENGTH(content) as content_length
            FROM reports {where}
            ORDER BY pinned DESC, created_at DESC
            LIMIT ?""",
        params,
    ).fetchall()

    conn.close()

    return [
        {
            "id": r["id"],
            "date": r["date"],
            "days": r["days"],
            "agent": r["agent"],
            "created_at": r["created_at"],
            "title": r["title"],
            "pinned": bool(r["pinned"]),
            "content_length": r["content_length"],
        }
        for r in rows
    ]


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    """Get a single report by ID, including full content."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "id": row["id"],
        "date": row["date"],
        "days": row["days"],
        "agent": row["agent"],
        "content": row["content"],
        "stats_json": json.loads(row["stats_json"]) if row["stats_json"] else None,
        "prompt": row["prompt"],
        "created_at": row["created_at"],
        "title": row["title"],
        "pinned": bool(row["pinned"]),
    }


def get_report_by_date(date: str) -> Optional[Dict[str, Any]]:
    """Get the most recent report for a specific date."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM reports WHERE date = ? ORDER BY created_at DESC LIMIT 1",
        (date,),
    ).fetchone()
    conn.close()

    if not row:
        return None

    return {
        "id": row["id"],
        "date": row["date"],
        "days": row["days"],
        "agent": row["agent"],
        "content": row["content"],
        "stats_json": json.loads(row["stats_json"]) if row["stats_json"] else None,
        "prompt": row["prompt"],
        "created_at": row["created_at"],
        "title": row["title"],
        "pinned": bool(row["pinned"]),
    }


def update_report(report_id: str, **kwargs) -> bool:
    """Update report fields (title, pinned, content)."""
    conn = _get_conn()
    allowed = {"title", "pinned", "content"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}

    if not updates:
        conn.close()
        return False

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    params = list(updates.values()) + [report_id]

    cursor = conn.execute(
        f"UPDATE reports SET {set_clause} WHERE id = ?", params
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def delete_report(report_id: str) -> bool:
    """Delete a report by ID."""
    conn = _get_conn()
    cursor = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted
