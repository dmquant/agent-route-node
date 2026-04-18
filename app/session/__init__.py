"""Session Layer — Durable Event Log

Sessions are append-only event streams, not chat history.
The session survives brain crashes. wake() + getEvents() = full recovery.
"""

from app.session.events import EventType, SessionEvent
from app.session.manager import SessionEventManager, session_events

__all__ = ["EventType", "SessionEvent", "SessionEventManager", "session_events"]
