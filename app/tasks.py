"""Background Task Manager — decouples execution from WebSocket viewing.

Sessions can run in the background. Switching the viewed session does not
interrupt running tasks. Each running task emits events to a queue that
any subscriber (WebSocket) can drain at any time.

Production Hardening (Phase 11):
- Task persistence: task records saved to SQLite task_log table
- Graceful shutdown: cancel running tasks on SIGTERM
- Periodic GC: auto-clean completed tasks from memory
- Stale recovery: orphaned tasks marked FAILED on startup
"""

import asyncio
import time
import uuid
import sqlite3
import os
import json
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, Any, List
from enum import Enum


class TaskPhase(str, Enum):
    """Execution phases for richer status reporting."""
    QUEUED = "queued"
    CONNECTING = "connecting"
    EXECUTING = "executing"
    STREAMING = "streaming"
    TOOL_CALLING = "tool_calling"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TaskStatus:
    """Live status of a running task."""
    task_id: str
    session_id: str
    agent: str
    prompt: str
    phase: TaskPhase
    started_at: float
    elapsed_ms: float = 0
    output_chunks: int = 0
    output_bytes: int = 0
    exit_code: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "agent": self.agent,
            "prompt": self.prompt[:80],
            "phase": self.phase.value,
            "started_at": self.started_at,
            "elapsed_ms": round(self.elapsed_ms),
            "output_chunks": self.output_chunks,
            "output_bytes": self.output_bytes,
            "exit_code": self.exit_code,
            "error": self.error,
        }


@dataclass
class BackgroundTask:
    """A background execution task with its event queue."""
    task_id: str
    session_id: str
    agent: str
    prompt: str
    status: TaskStatus
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    asyncio_task: Optional[asyncio.Task] = None
    subscribers: List[asyncio.Queue] = field(default_factory=list)

    def add_subscriber(self, q: asyncio.Queue):
        """Add a WebSocket subscriber to receive events."""
        self.subscribers.append(q)

    def remove_subscriber(self, q: asyncio.Queue):
        """Remove a subscriber."""
        if q in self.subscribers:
            self.subscribers.remove(q)

    async def broadcast(self, event: dict):
        """Send event to all subscribers."""
        for q in self.subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop if subscriber is slow


# ─── SQLite persistence for task history ─────────────────

from app.config import get_db_path
DB_PATH = get_db_path()

def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_task_tables():
    """Create the task_log table for persistent task history."""
    conn = _get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_log (
            task_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            agent TEXT NOT NULL,
            prompt TEXT NOT NULL,
            phase TEXT NOT NULL DEFAULT 'queued',
            started_at REAL NOT NULL,
            finished_at REAL,
            elapsed_ms REAL DEFAULT 0,
            output_chunks INTEGER DEFAULT 0,
            output_bytes INTEGER DEFAULT 0,
            exit_code INTEGER,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_log_session ON task_log(session_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_log_phase ON task_log(phase)
    """)
    # Mark any orphaned running tasks as failed (stale recovery)
    orphaned = conn.execute(
        "UPDATE task_log SET phase = 'failed', error = 'Server restarted — task orphaned' "
        "WHERE phase NOT IN ('completed', 'failed')"
    )
    if orphaned.rowcount > 0:
        print(f"[TaskManager] Recovered {orphaned.rowcount} orphaned task(s) from previous run")
    conn.commit()
    conn.close()

def _persist_task(status: TaskStatus):
    """Save or update a task record in SQLite."""
    conn = _get_connection()
    conn.execute("""
        INSERT INTO task_log (task_id, session_id, agent, prompt, phase, started_at, elapsed_ms, output_chunks, output_bytes, exit_code, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_id) DO UPDATE SET
            phase = excluded.phase,
            elapsed_ms = excluded.elapsed_ms,
            output_chunks = excluded.output_chunks,
            output_bytes = excluded.output_bytes,
            exit_code = excluded.exit_code,
            error = excluded.error,
            finished_at = CASE WHEN excluded.phase IN ('completed', 'failed') THEN datetime('now') ELSE finished_at END
    """, (
        status.task_id, status.session_id, status.agent,
        status.prompt[:500], status.phase.value, status.started_at,
        round(status.elapsed_ms), status.output_chunks, status.output_bytes,
        status.exit_code, status.error,
    ))
    conn.commit()
    conn.close()

def get_task_history(session_id: Optional[str] = None, limit: int = 50) -> List[dict]:
    """Query task history from SQLite."""
    conn = _get_connection()
    if session_id:
        rows = conn.execute(
            "SELECT * FROM task_log WHERE session_id = ? ORDER BY started_at DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_log ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class BackgroundTaskManager:
    """Manage running tasks across sessions.

    - Tasks run independently of WebSocket connections
    - Switching sessions does NOT stop running tasks
    - WebSocket subscribers can attach/detach to any running task
    - Status updates and output are buffered for late joiners
    - Task records persist to SQLite for history and recovery
    """

    def __init__(self):
        self._tasks: Dict[str, BackgroundTask] = {}
        # session_id → list of active task_ids
        self._session_tasks: Dict[str, List[str]] = {}
        # Global subscriber for all events (the main WS connection)
        self._global_subscribers: List[asyncio.Queue] = []
        # GC task handle
        self._gc_task: Optional[asyncio.Task] = None

    def create_task(
        self,
        session_id: str,
        agent: str,
        prompt: str,
    ) -> BackgroundTask:
        """Create a new background task (but don't start it yet)."""
        task_id = uuid.uuid4().hex[:12]
        now = time.time() * 1000

        status = TaskStatus(
            task_id=task_id,
            session_id=session_id,
            agent=agent,
            prompt=prompt,
            phase=TaskPhase.QUEUED,
            started_at=now,
        )

        bg_task = BackgroundTask(
            task_id=task_id,
            session_id=session_id,
            agent=agent,
            prompt=prompt,
            status=status,
        )

        self._tasks[task_id] = bg_task

        if session_id not in self._session_tasks:
            self._session_tasks[session_id] = []
        self._session_tasks[session_id].append(task_id)

        # Persist to SQLite
        _persist_task(status)

        return bg_task

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def get_session_tasks(self, session_id: str) -> List[BackgroundTask]:
        """Get all tasks (running or completed) for a session."""
        task_ids = self._session_tasks.get(session_id, [])
        return [self._tasks[tid] for tid in task_ids if tid in self._tasks]

    def get_running_tasks(self) -> List[BackgroundTask]:
        """Get all currently running tasks across all sessions."""
        return [
            t for t in self._tasks.values()
            if t.status.phase not in (TaskPhase.COMPLETED, TaskPhase.FAILED)
        ]

    def get_running_session_ids(self) -> List[str]:
        """Get session IDs that have actively running tasks."""
        return list(set(
            t.session_id for t in self._tasks.values()
            if t.status.phase not in (TaskPhase.COMPLETED, TaskPhase.FAILED)
        ))

    async def update_phase(self, task_id: str, phase: TaskPhase, **extra):
        """Update task phase and broadcast to subscribers."""
        task = self._tasks.get(task_id)
        if not task:
            return

        task.status.phase = phase
        task.status.elapsed_ms = time.time() * 1000 - task.status.started_at

        if phase == TaskPhase.COMPLETED:
            task.status.exit_code = extra.get("exit_code", 0)
        elif phase == TaskPhase.FAILED:
            task.status.exit_code = extra.get("exit_code", 1)
            task.status.error = extra.get("error", "Unknown error")

        event = {
            "type": "task_status",
            "taskId": task_id,
            "sessionId": task.session_id,
            **task.status.to_dict(),
        }

        await task.broadcast(event)
        await self._broadcast_global(event)

        # Persist phase transitions to SQLite (terminal phases always persist)
        if phase in (TaskPhase.COMPLETED, TaskPhase.FAILED, TaskPhase.CONNECTING, TaskPhase.EXECUTING):
            _persist_task(task.status)

    async def emit_output(self, task_id: str, chunk: str, source: str = "agent"):
        """Emit an output chunk and broadcast."""
        task = self._tasks.get(task_id)
        if not task:
            return

        task.status.output_chunks += 1
        task.status.output_bytes += len(chunk)
        task.status.elapsed_ms = time.time() * 1000 - task.status.started_at

        event = {
            "type": "node_execution_log",
            "taskId": task_id,
            "sessionId": task.session_id,
            "nodeId": task_id,
            "log": chunk,
            "source": source,
        }

        await task.broadcast(event)
        await self._broadcast_global(event)

    async def emit_event(self, task_id: str, event: dict):
        """Emit arbitrary event (images, completion, etc.)."""
        task = self._tasks.get(task_id)
        if not task:
            return

        event["taskId"] = task_id
        event["sessionId"] = task.session_id

        await task.broadcast(event)
        await self._broadcast_global(event)

    def add_global_subscriber(self, q: asyncio.Queue):
        """Subscribe to all task events (main WebSocket connection)."""
        self._global_subscribers.append(q)

    def remove_global_subscriber(self, q: asyncio.Queue):
        if q in self._global_subscribers:
            self._global_subscribers.remove(q)

    async def _broadcast_global(self, event: dict):
        for q in self._global_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def cleanup_completed(self, max_age_ms: float = 300000):
        """Remove completed tasks older than max_age_ms (default 5 min)."""
        now = time.time() * 1000
        to_remove = []
        for tid, task in self._tasks.items():
            if task.status.phase in (TaskPhase.COMPLETED, TaskPhase.FAILED):
                if now - task.status.started_at > max_age_ms:
                    to_remove.append(tid)

        for tid in to_remove:
            task = self._tasks.pop(tid, None)
            if task and task.session_id in self._session_tasks:
                self._session_tasks[task.session_id] = [
                    t for t in self._session_tasks[task.session_id] if t != tid
                ]

    def get_all_status(self) -> List[dict]:
        """Get status of all active tasks."""
        now = time.time() * 1000
        result = []
        for t in self._tasks.values():
            t.status.elapsed_ms = now - t.status.started_at
            result.append(t.status.to_dict())
        return result

    # ─── Graceful Shutdown ──────────────────────────────────
    async def shutdown(self):
        """Cancel all running tasks and persist their final state."""
        running = self.get_running_tasks()
        if not running:
            return

        print(f"[TaskManager] Graceful shutdown: cancelling {len(running)} running task(s)...")

        for task in running:
            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()

            task.status.phase = TaskPhase.FAILED
            task.status.error = "Server shutdown"
            task.status.exit_code = 130  # SIGINT convention
            task.status.elapsed_ms = time.time() * 1000 - task.status.started_at
            _persist_task(task.status)

        # Wait briefly for cancellations to propagate
        await asyncio.sleep(0.5)
        print("[TaskManager] Shutdown complete.")

    # ─── Periodic GC ──────────────────────────────────
    async def start_gc_loop(self, interval_seconds: float = 60.0, max_age_ms: float = 300000):
        """Start periodic garbage collection of completed tasks."""
        async def _gc():
            while True:
                await asyncio.sleep(interval_seconds)
                self.cleanup_completed(max_age_ms)

        self._gc_task = asyncio.create_task(_gc())

    def stop_gc_loop(self):
        """Stop the GC loop."""
        if self._gc_task and not self._gc_task.done():
            self._gc_task.cancel()


# Global singleton
task_manager = BackgroundTaskManager()
