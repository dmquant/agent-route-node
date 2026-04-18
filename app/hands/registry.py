"""Hand Registry — manages all available hands.

Cattle, not pets: every hand is interchangeable and replaceable.
"""

import asyncio
from typing import Optional, Dict, List
from app.hands.base import Hand


class HandRegistry:
    """Central registry of all available execution hands."""

    def __init__(self):
        self._hands: Dict[str, Hand] = {}

    def register(self, hand: Hand) -> None:
        """Register a hand by its name."""
        self._hands[hand.name] = hand
        print(f"[HandRegistry] Registered: {hand}")

    def get(self, name: str) -> Optional[Hand]:
        """Get a hand by name. Returns None if not found."""
        return self._hands.get(name)

    def list_all(self) -> List[Hand]:
        """List all registered hands."""
        return list(self._hands.values())

    def list_names(self) -> List[str]:
        """List all registered hand names."""
        return list(self._hands.keys())

    def list_info(self) -> List[dict]:
        """Serializable list of hand metadata."""
        return [h.info() for h in self._hands.values()]

    def mark_rate_limited(self, name: str, retry_after: int) -> None:
        """Mark an agent as rate limited until the given time."""
        import time
        if not hasattr(self, "_rate_limits"):
            self._rate_limits = {}
        self._rate_limits[name] = time.time() + retry_after
        print(f"[HandRegistry] {name} rate limited for {retry_after}s.")

    def is_available(self, name: str) -> bool:
        """Check if agent is available (not rate limited)."""
        import time
        if not hasattr(self, "_rate_limits"):
            self._rate_limits = {}
        until = self._rate_limits.get(name)
        if until:
            if time.time() < until:
                return False
            del self._rate_limits[name]
        return True

    def get_available(self, name: str, backups: List[str] = None) -> Optional[Hand]:
        """Get the primary agent if available, else try backups in order."""
        if self.is_available(name):
            return self.get(name)
        if backups:
            for b in backups:
                if self.is_available(b):
                    print(f"[HandRegistry] {name} rate limited, falling back to {b}")
                    return self.get(b)
        return None

    async def health_check_all(self) -> Dict[str, dict]:
        """Check health of all registered hands concurrently.
        
        Returns a dict like: {"gemini": {"healthy": True, "enabled": True}, ...}
        - enabled: Whether the agent route is enabled in .env
        - healthy: Whether the hand binary/service is actually reachable
        """
        import os

        _ENV_GATES = {
            "gemini": "ENABLE_GEMINI_CLI",
            "claude": "ENABLE_CLAUDE_REMOTE_CONTROL",
            "codex": "ENABLE_CODEX_SERVER",
            "ollama": "ENABLE_OLLAMA_API",
            "mflux": "ENABLE_MFLUX_IMAGE",
        }

        results: Dict[str, dict] = {}

        async def check_one(name: str, hand: Hand):
            env_key = _ENV_GATES.get(name, "")
            enabled = os.getenv(env_key) == "true" if env_key else True
            if not enabled:
                results[name] = {"healthy": False, "enabled": False}
                return
            try:
                healthy = await asyncio.wait_for(hand.health_check(), timeout=10)
                results[name] = {"healthy": healthy, "enabled": True}
            except Exception:
                results[name] = {"healthy": False, "enabled": True}

        await asyncio.gather(
            *[check_one(n, h) for n, h in self._hands.items()]
        )
        return results

    def __len__(self):
        return len(self._hands)


# ─── Global Singleton ──────────────────────
hand_registry = HandRegistry()


def auto_register_all():
    """Auto-discover and register all built-in hands."""
    from app.hands.gemini_hand import GeminiHand
    from app.hands.claude_hand import ClaudeHand
    from app.hands.codex_hand import CodexHand
    from app.hands.ollama_hand import OllamaHand
    from app.hands.mflux_hand import MfluxHand

    for HandClass in [GeminiHand, ClaudeHand, CodexHand, OllamaHand, MfluxHand]:
        hand_registry.register(HandClass())

    print(f"[HandRegistry] {len(hand_registry)} hands registered: {hand_registry.list_names()}")
