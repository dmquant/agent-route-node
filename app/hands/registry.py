"""Hand Registry — manages all available hands and their rate-limit state.

Cattle, not pets: every hand is interchangeable and replaceable.

Rate-limit cooldowns are persisted to ~/.agent-route/rate_limits.json so a
node restart doesn't reset a 15-hour gemini cooldown to "available". The
file is the source of truth — the in-memory dict is just a fast cache.

Fallback chains (gemini ↔ claude ↔ codex) live in this module so callers
don't have to repeat the list at every call site. Other hands (vane, mflux,
ollama, opencode) have NO default fallback — they're not interchangeable
capabilities, only the three coding CLIs are.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Optional, Dict, List, Tuple

from app.hands.base import Hand
from app.hands.rate_limit import RateLimitInfo

# Default fallback chains for the three coding CLIs. The first hand in the
# list is the primary; if cool, try the next, then the next.
#
# A new hand is added by registering it AND adding it to this map. Hands
# absent from the map have NO automatic fallback — explicit user choice.
DEFAULT_FALLBACK_CHAINS: Dict[str, List[str]] = {
    "gemini": ["claude", "codex"],
    "claude": ["gemini", "codex"],
    "codex":  ["claude", "gemini"],
}

# Env gates control which hands actually get registered & report healthy.
_ENV_GATES = {
    "gemini": "ENABLE_GEMINI_CLI",
    "claude": "ENABLE_CLAUDE_REMOTE_CONTROL",
    "codex":  "ENABLE_CODEX_SERVER",
    "ollama": "ENABLE_OLLAMA_API",
    "mflux":  "ENABLE_MFLUX_IMAGE",
    "vane":   "ENABLE_VANE_SEARCH",
    "opencode": "ENABLE_OPENCODE",
}


def _cooldowns_path() -> str:
    """Where the cooldown file lives. Late-import to avoid module cycles."""
    from app.config import get_data_dir
    return os.path.join(get_data_dir(), "rate_limits.json")


class HandRegistry:
    """Central registry of all available execution hands."""

    def __init__(self):
        self._hands: Dict[str, Hand] = {}
        self._cooldowns: Dict[str, dict] = {}  # name → {until, reason, marked_at}
        self._cooldowns_loaded = False

    # ─── Registration ────────────────────────────────────────────────

    def register(self, hand: Hand) -> None:
        """Register a hand by its name."""
        self._hands[hand.name] = hand
        print(f"[HandRegistry] Registered: {hand}")

    def get(self, name: str) -> Optional[Hand]:
        """Get a hand by name. Returns None if not registered."""
        return self._hands.get(name)

    def list_all(self) -> List[Hand]:
        return list(self._hands.values())

    def list_names(self) -> List[str]:
        return list(self._hands.keys())

    def list_info(self) -> List[dict]:
        return [h.info() for h in self._hands.values()]

    # ─── Cooldown persistence ────────────────────────────────────────

    def _load_cooldowns(self) -> None:
        """Load cooldowns from disk. Idempotent; cleans expired entries."""
        if self._cooldowns_loaded:
            return
        path = _cooldowns_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or {}
                now = time.time()
                self._cooldowns = {
                    name: entry
                    for name, entry in raw.items()
                    if isinstance(entry, dict) and entry.get("until", 0) > now
                }
                if self._cooldowns:
                    print(f"[HandRegistry] Loaded {len(self._cooldowns)} active cooldown(s) from {path}")
        except Exception as e:
            print(f"[HandRegistry] Could not load cooldowns from {path}: {e}")
            self._cooldowns = {}
        self._cooldowns_loaded = True

    def _save_cooldowns(self) -> None:
        """Atomic write of the cooldown file."""
        path = _cooldowns_path()
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cooldowns, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[HandRegistry] Failed to persist cooldowns: {e}")

    # ─── Rate-limit API ──────────────────────────────────────────────

    def mark_rate_limited(
        self,
        name: str,
        retry_after: int,
        reason: str = "rate_limited",
    ) -> None:
        """Mark a hand as rate-limited for `retry_after` seconds.

        Persists to disk so restarts don't clobber the cooldown. If a
        cooldown already exists, only update if the new one ends LATER
        (don't shorten an existing window).
        """
        self._load_cooldowns()
        until = time.time() + max(60, int(retry_after))  # never shorter than 1min
        existing = self._cooldowns.get(name) or {}
        if existing.get("until", 0) >= until:
            # Existing cooldown is already at least as long — keep it.
            return
        self._cooldowns[name] = {
            "until": until,
            "reason": reason,
            "marked_at": int(time.time()),
        }
        self._save_cooldowns()
        eta_str = _human_eta(retry_after)
        print(f"[HandRegistry] {name} → cooldown for {eta_str} (reason={reason})")

    def mark_rate_limited_from(self, info: RateLimitInfo) -> None:
        """Convenience: take a RateLimitInfo from rate_limit.parse_rate_limit."""
        self.mark_rate_limited(info.hand, info.retry_after_s, info.reason)

    def clear_cooldown(self, name: str) -> None:
        """Remove any active cooldown for a hand. Used by ops/UI."""
        self._load_cooldowns()
        if name in self._cooldowns:
            del self._cooldowns[name]
            self._save_cooldowns()

    def cooldown_info(self, name: str) -> Optional[dict]:
        """Return the active cooldown entry for a hand, or None."""
        self._load_cooldowns()
        entry = self._cooldowns.get(name)
        if not entry:
            return None
        if entry["until"] <= time.time():
            del self._cooldowns[name]
            self._save_cooldowns()
            return None
        return entry

    def is_available(self, name: str) -> bool:
        """A hand is available iff registered, env-enabled, AND not in cooldown."""
        if name not in self._hands:
            return False
        if not _is_enabled(name):
            return False
        return self.cooldown_info(name) is None

    def get_available(
        self,
        name: str,
        backups: Optional[List[str]] = None,
    ) -> Optional[Hand]:
        """Resolve a hand request to a usable hand, falling back if needed.

        Returns None when the primary AND every backup is unavailable. The
        caller should treat that as "all exhausted" and surface a 429-style
        signal upstream.
        """
        if self.is_available(name):
            return self.get(name)
        chain = backups if backups is not None else DEFAULT_FALLBACK_CHAINS.get(name, [])
        for b in chain:
            if self.is_available(b):
                print(f"[HandRegistry] {name} unavailable → falling back to {b}")
                return self.get(b)
        return None

    def resolve(
        self,
        name: str,
        allow_fallback: bool = True,
    ) -> Tuple[Optional[Hand], List[str]]:
        """Like get_available but returns (hand, tried_names) for traceability.

        `tried_names` lists every hand we examined, in order — useful for
        building the structured output prefix when all options are exhausted.
        """
        tried: List[str] = []

        def _try(n: str) -> Optional[Hand]:
            tried.append(n)
            if self.is_available(n):
                return self.get(n)
            return None

        primary = _try(name)
        if primary or not allow_fallback:
            return primary, tried
        for b in DEFAULT_FALLBACK_CHAINS.get(name, []):
            h = _try(b)
            if h:
                return h, tried
        return None, tried

    # ─── Status snapshot (for /api/hands/status + heartbeat) ─────────

    def status_snapshot(self) -> Dict[str, dict]:
        """Per-hand availability map, suitable for JSON serialization."""
        self._load_cooldowns()
        now = time.time()
        out: Dict[str, dict] = {}
        for name, hand in self._hands.items():
            cd = self._cooldowns.get(name)
            if cd and cd["until"] <= now:
                cd = None  # expired but not yet swept
            out[name] = {
                "registered": True,
                "enabled": _is_enabled(name),
                "available": _is_enabled(name) and cd is None,
                "cooldown_until": int(cd["until"]) if cd else None,
                "retry_after_s": max(0, int(cd["until"] - now)) if cd else 0,
                "reason": cd.get("reason") if cd else None,
                "fallback_chain": DEFAULT_FALLBACK_CHAINS.get(name, []),
            }
        return out

    # ─── Health checks ───────────────────────────────────────────────

    async def health_check_all(self) -> Dict[str, dict]:
        """Probe every registered hand's binary/service liveness concurrently."""
        results: Dict[str, dict] = {}

        async def check_one(name: str, hand: Hand):
            enabled = _is_enabled(name)
            if not enabled:
                results[name] = {"healthy": False, "enabled": False}
                return
            try:
                healthy = await asyncio.wait_for(hand.health_check(), timeout=10)
                results[name] = {"healthy": healthy, "enabled": True}
            except Exception:
                results[name] = {"healthy": False, "enabled": True}

        await asyncio.gather(*[check_one(n, h) for n, h in self._hands.items()])
        return results

    def __len__(self):
        return len(self._hands)


# ─── Helpers ─────────────────────────────────────────────────────────────


def _is_enabled(name: str) -> bool:
    env_key = _ENV_GATES.get(name, "")
    return os.getenv(env_key) == "true" if env_key else True


def _human_eta(seconds: int) -> str:
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60}m{s % 60}s"
    return f"{s}s"


# ─── Global Singleton ────────────────────────────────────────────────────
hand_registry = HandRegistry()


def auto_register_all():
    """Auto-discover and register all built-in hands."""
    from app.hands.gemini_hand import GeminiHand
    from app.hands.claude_hand import ClaudeHand
    from app.hands.codex_hand import CodexHand
    from app.hands.ollama_hand import OllamaHand
    from app.hands.mflux_hand import MfluxHand
    from app.hands.vane_hand import VaneHand
    from app.hands.opencode_hand import OpencodeHand

    for HandClass in [GeminiHand, ClaudeHand, CodexHand, OllamaHand, MfluxHand, VaneHand, OpencodeHand]:
        hand_registry.register(HandClass())

    # Eagerly load any persisted cooldowns so /api/hands/status is accurate
    # immediately after startup (don't wait for first task).
    hand_registry._load_cooldowns()

    print(f"[HandRegistry] {len(hand_registry)} hands registered: {hand_registry.list_names()}")
