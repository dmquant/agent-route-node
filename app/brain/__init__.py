"""Brain Layer — Stateless Orchestrator

The brain is cattle. If it crashes, wake() + getEvents() resumes.
Brains call hands without knowing the transport underneath.
"""

from app.brain.orchestrator import AgentOrchestrator
from app.brain.harness import HarnessManager, HarnessConfig
from app.brain.context import ContextEngine

__all__ = ["AgentOrchestrator", "HarnessManager", "HarnessConfig", "ContextEngine"]
