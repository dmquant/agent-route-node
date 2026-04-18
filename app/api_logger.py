"""API Call Logger — records every HTTP request to api_bridge.

Provides a full audit trail of all API interactions, whether from the
frontend UI, external scripts (curl), or programmatic SDK clients.
This enables the Brain Inspector and Dashboard to show a unified view
of all agent activity, not just UI-triggered runs.

Tables:
  - api_call_log: Every HTTP request with method, path, status, duration
  - api_executions: Execution-specific records linking direct API calls
    to sessions for visibility in the session list and Brain Inspector
"""

import time
import uuid
import sqlite3
import os
import json
from typing import Optional, List, Dict, Any
from collections import defaultdict
from datetime import datetime, timedelta


from app.config import get_db_path
DB_PATH = get_db_path()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_api_log_tables():
    """Create tables for API call logging."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS api_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            query_params TEXT DEFAULT '',
            status_code INTEGER DEFAULT 0,
            duration_ms REAL DEFAULT 0,
            client_ip TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            request_body_preview TEXT DEFAULT '',
            response_preview TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            agent TEXT DEFAULT '',
            source TEXT DEFAULT 'api',
            category TEXT DEFAULT 'other',
            created_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_api_log_created
            ON api_call_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_api_log_category
            ON api_call_log(category);
        CREATE INDEX IF NOT EXISTS idx_api_log_session
            ON api_call_log(session_id);
        CREATE INDEX IF NOT EXISTS idx_api_log_path
            ON api_call_log(path);
    """)
    conn.commit()
    conn.close()
    print("[APICallLogger] Tables initialized.")


# ─── Categorization ──────────────────────────────────────

def _categorize_path(method: str, path: str) -> str:
    """Classify an API path into a logical category."""
    if path.startswith("/execute") or path.startswith("/api/multi-agent"):
        return "execution"
    if "/brain/" in path:
        return "brain"
    if "/workflow" in path:
        return "workflow"
    if "/sessions/" in path and method in ("POST", "PUT", "DELETE"):
        return "session_mutation"
    if "/sessions" in path:
        return "session_read"
    if "/context-links" in path or "/fork" in path or "/linked-messages" in path:
        return "context"
    if "/reports" in path:
        return "report"
    if "/agents" in path or "/hands" in path:
        return "agent"
    if "/tasks" in path or "/analytics" in path:
        return "analytics"
    if "/sandboxes" in path:
        return "sandbox"
    if "/upload" in path or "/files" in path or "/workspace" in path:
        return "file"
    if path == "/ws/agent":
        return "websocket"
    return "other"


def _extract_session_id(path: str) -> str:
    """Try to extract session_id from known URL patterns."""
    parts = path.split("/")
    # /api/sessions/{id}/... or /api/brain/{id}/...
    for i, p in enumerate(parts):
        if p in ("sessions", "brain") and i + 1 < len(parts):
            candidate = parts[i + 1]
            # Skip sub-resource names
            if candidate not in ("", "context", "status", "events", "messages",
                                  "workspace", "files", "wake", "run", "pause",
                                  "delegate", "checkpoint", "summary"):
                return candidate
    return ""


def _extract_agent(path: str, body: dict) -> str:
    """Try to extract agent name from the request."""
    agent = body.get("client", "") or body.get("agent", "")
    if not agent:
        agents = body.get("agents", [])
        if agents:
            agent = ",".join(agents)
    return agent


# ─── CRUD ──────────────────────────────────────

def record_api_call(
    request_id: str,
    method: str,
    path: str,
    query_params: str = "",
    status_code: int = 0,
    duration_ms: float = 0,
    client_ip: str = "",
    user_agent: str = "",
    request_body_preview: str = "",
    response_preview: str = "",
    session_id: str = "",
    agent: str = "",
    source: str = "api",
) -> None:
    """Record a single API call to the log."""
    category = _categorize_path(method, path)
    if not session_id:
        session_id = _extract_session_id(path)

    conn = _get_conn()
    conn.execute(
        """INSERT INTO api_call_log
           (request_id, method, path, query_params, status_code, duration_ms,
            client_ip, user_agent, request_body_preview, response_preview,
            session_id, agent, source, category, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            request_id, method, path, query_params, status_code, duration_ms,
            client_ip, user_agent,
            request_body_preview[:500], response_preview[:500],
            session_id, agent, source, category,
            time.time() * 1000,
        ),
    )
    conn.commit()
    conn.close()


def get_api_calls(
    limit: int = 100,
    category: Optional[str] = None,
    session_id: Optional[str] = None,
    since_ms: Optional[float] = None,
    source: Optional[str] = None,
    method: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Query API call log with optional filters."""
    conn = _get_conn()
    conditions = []
    params: list = []

    if category:
        conditions.append("category = ?")
        params.append(category)
    if session_id:
        conditions.append("session_id = ?")
        params.append(session_id)
    if since_ms:
        conditions.append("created_at > ?")
        params.append(since_ms)
    if source:
        conditions.append("source = ?")
        params.append(source)
    if method:
        conditions.append("method = ?")
        params.append(method)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    rows = conn.execute(
        f"SELECT * FROM api_call_log {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_api_stats(hours: int = 24) -> Dict[str, Any]:
    """Aggregate API call statistics."""
    conn = _get_conn()
    cutoff = (time.time() - hours * 3600) * 1000

    rows = conn.execute(
        "SELECT * FROM api_call_log WHERE created_at > ? ORDER BY created_at DESC",
        (cutoff,),
    ).fetchall()
    conn.close()

    if not rows:
        return {
            "total_calls": 0, "period_hours": hours,
            "by_category": {}, "by_method": {}, "by_source": {},
            "by_status": {}, "avg_duration_ms": 0,
            "execution_calls": 0, "unique_sessions": 0,
        }

    calls = [dict(r) for r in rows]

    by_category: Dict[str, int] = defaultdict(int)
    by_method: Dict[str, int] = defaultdict(int)
    by_source: Dict[str, int] = defaultdict(int)
    by_status: Dict[str, int] = defaultdict(int)
    durations = []
    sessions = set()

    for c in calls:
        by_category[c["category"]] += 1
        by_method[c["method"]] += 1
        by_source[c["source"]] += 1
        status_bucket = f"{c['status_code'] // 100}xx"
        by_status[status_bucket] += 1
        if c["duration_ms"] and c["duration_ms"] > 0:
            durations.append(c["duration_ms"])
        if c["session_id"]:
            sessions.add(c["session_id"])

    return {
        "total_calls": len(calls),
        "period_hours": hours,
        "by_category": dict(by_category),
        "by_method": dict(by_method),
        "by_source": dict(by_source),
        "by_status": dict(by_status),
        "avg_duration_ms": round(sum(durations) / len(durations)) if durations else 0,
        "execution_calls": by_category.get("execution", 0),
        "unique_sessions": len(sessions),
        "top_paths": _top_paths(calls),
    }


def _top_paths(calls: list, top_n: int = 10) -> list:
    """Most frequently called paths."""
    path_counts: Dict[str, int] = defaultdict(int)
    for c in calls:
        path_counts[c["path"]] += 1
    return sorted(
        [{"path": p, "count": n} for p, n in path_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:top_n]


def get_api_timeline(hours: int = 1, bucket_minutes: int = 5) -> List[Dict[str, Any]]:
    """Get API call frequency over time for live monitoring."""
    conn = _get_conn()
    cutoff = (time.time() - hours * 3600) * 1000

    rows = conn.execute(
        "SELECT created_at, category, method FROM api_call_log WHERE created_at > ?",
        (cutoff,),
    ).fetchall()
    conn.close()

    bucket_ms = bucket_minutes * 60 * 1000
    buckets: Dict[int, Dict[str, int]] = defaultdict(lambda: {"total": 0, "execution": 0, "other": 0})

    for r in rows:
        ts = int(r["created_at"])
        bucket_key = (ts // bucket_ms) * bucket_ms
        buckets[bucket_key]["total"] += 1
        if r["category"] == "execution":
            buckets[bucket_key]["execution"] += 1
        else:
            buckets[bucket_key]["other"] += 1

    return sorted(
        [{"timestamp": k, **v} for k, v in buckets.items()],
        key=lambda x: x["timestamp"],
    )
