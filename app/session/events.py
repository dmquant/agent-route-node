"""Session Event Types — structured, append-only event log.

Key insight from Anthropic: Sessions should be append-only *event logs*
with structured types, not just user/assistant message pairs. This enables:

- Rewinding to see lead-up before a specific event
- Picking up from where the brain last stopped reading
- Filtering by event type (only tool results, only errors, etc.)
- Full crash recovery via wake(sessionId) + getEvents()
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Optional
import time
import json


class EventType(str, Enum):
    """All possible session event types."""

    # ── Core lifecycle ────────────────
    SESSION_CREATED = "session.created"
    SESSION_RESUMED = "session.resumed"
    SESSION_PAUSED = "session.paused"

    # ── Messages ──────────────────────
    USER_MESSAGE = "message.user"
    AGENT_RESPONSE = "message.agent"

    # ── Tool execution ────────────────
    # execute(name, input) → string
    TOOL_CALL = "tool.call"          # Brain → Hand
    TOOL_RESULT = "tool.result"      # Hand → Brain
    TOOL_ERROR = "tool.error"        # Hand failure

    # ── Context management ────────────
    CONTEXT_COMPACT = "context.compact"
    CONTEXT_RESET = "context.reset"
    CONTEXT_CHECKPOINT = "context.checkpoint"

    # ── Agent routing ─────────────────
    AGENT_SELECTED = "agent.selected"
    AGENT_DELEGATED = "agent.delegated"   # Brain passes hand to sub-brain
    AGENT_JOINED = "agent.joined"          # Sub-brain returns results

    # ── Workspace / Sandbox ───────────
    SANDBOX_PROVISIONED = "sandbox.provisioned"
    SANDBOX_DESTROYED = "sandbox.destroyed"
    FILE_CREATED = "file.created"
    FILE_MODIFIED = "file.modified"

    # ── System ────────────────────────
    ERROR = "error"
    METRIC = "metric"   # Token counts, latency, etc.


@dataclass
class SessionEvent:
    """A single immutable event in the session log.

    Events are append-only and never modified. The session event log
    is the single source of truth for session state recovery.
    """
    id: int                           # Auto-increment position
    session_id: str
    event_type: EventType
    agent: Optional[str] = None       # Which hand/brain produced this
    content: str = ""                 # Main payload text
    metadata: dict = field(default_factory=dict)  # Structured data (JSON)
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "event_type": self.event_type.value if isinstance(self.event_type, EventType) else self.event_type,
            "agent": self.agent,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @staticmethod
    def from_row(row: dict) -> "SessionEvent":
        """Create from a SQLite row dict."""
        meta = row.get("metadata", "{}")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        return SessionEvent(
            id=row["id"],
            session_id=row["session_id"],
            event_type=row["event_type"],
            agent=row.get("agent"),
            content=row.get("content", ""),
            metadata=meta,
            timestamp=row.get("timestamp", 0),
        )
