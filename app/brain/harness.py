"""Harness Manager — select, configure, and swap agent harnesses.

Key insight from Anthropic: harnesses encode assumptions that go stale
as models improve. The HarnessManager makes those assumptions explicit
and swappable without disrupting sessions.
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass
class HarnessConfig:
    """Configuration for a specific agent harness.

    These parameters control how the brain interacts with a hand:
    context limits, compaction policies, retry behavior, timeouts.
    """
    agent: str
    max_context_tokens: int = 100000
    auto_compact: bool = True
    compact_threshold: float = 0.8       # Compact at 80% context usage
    compact_strategy: str = "tail"       # "full" | "tail" | "summary"
    retry_on_failure: bool = True
    max_retries: int = 3
    timeout_seconds: int = 300
    skills: List[str] = field(default_factory=list)
    env_gate: Optional[str] = None       # .env variable that enables this hand

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "max_context_tokens": self.max_context_tokens,
            "auto_compact": self.auto_compact,
            "compact_threshold": self.compact_threshold,
            "compact_strategy": self.compact_strategy,
            "retry_on_failure": self.retry_on_failure,
            "max_retries": self.max_retries,
            "timeout_seconds": self.timeout_seconds,
            "skills": self.skills,
        }


class HarnessManager:
    """Select, configure, and swap harnesses at runtime.

    Harnesses encode assumptions about each agent's strengths and limits.
    These assumptions are explicit and hot-swappable: change a config
    without restarting the brain or disrupting a session.
    """

    def __init__(self):
        self._configs: Dict[str, HarnessConfig] = {}
        self._init_defaults()

    def _init_defaults(self):
        """Bootstrap with sensible defaults per agent type."""
        self._configs["gemini"] = HarnessConfig(
            agent="gemini",
            max_context_tokens=1_000_000,   # Gemini Pro: 1M context
            auto_compact=False,              # Rarely needed at 1M
            compact_threshold=0.9,
            timeout_seconds=120,
            env_gate="ENABLE_GEMINI_CLI",
        )
        self._configs["claude"] = HarnessConfig(
            agent="claude",
            max_context_tokens=200_000,     # Claude: 200k context
            auto_compact=True,
            compact_threshold=0.7,           # Start compacting early
            compact_strategy="summary",
            timeout_seconds=180,
            env_gate="ENABLE_CLAUDE_REMOTE_CONTROL",
        )
        self._configs["codex"] = HarnessConfig(
            agent="codex",
            max_context_tokens=128_000,
            auto_compact=True,
            compact_threshold=0.75,
            timeout_seconds=120,
            env_gate="ENABLE_CODEX_SERVER",
        )
        self._configs["ollama"] = HarnessConfig(
            agent="ollama",
            max_context_tokens=8_000,        # Local models: small context
            auto_compact=True,
            compact_threshold=0.5,           # Compact aggressively
            compact_strategy="tail",         # Just keep recent
            timeout_seconds=300,             # Slower on local hardware
            env_gate="ENABLE_OLLAMA_API",
        )
        self._configs["mflux"] = HarnessConfig(
            agent="mflux",
            max_context_tokens=0,            # Image gen: no context
            auto_compact=False,
            timeout_seconds=60,
            env_gate="ENABLE_MFLUX_IMAGE",
        )

    def select(self, agent: str) -> HarnessConfig:
        """Get the harness config for an agent. Returns default if unknown."""
        return self._configs.get(agent, HarnessConfig(agent=agent))

    def configure(self, agent: str, **overrides) -> HarnessConfig:
        """Update a harness config with new values."""
        config = self.select(agent)
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)
        self._configs[agent] = config
        return config

    def swap(self, agent: str, new_config: HarnessConfig) -> HarnessConfig:
        """Replace a harness config entirely."""
        old = self._configs.get(agent)
        self._configs[agent] = new_config
        return old

    def list_configs(self) -> Dict[str, dict]:
        """Serializable view of all harness configs."""
        return {name: cfg.to_dict() for name, cfg in self._configs.items()}

    def get_context_budget(self, agent: str) -> int:
        """How many tokens can this agent's context window hold?"""
        cfg = self.select(agent)
        return int(cfg.max_context_tokens * cfg.compact_threshold)


# ─── Global Singleton ──────────────────────
harness_manager = HarnessManager()
