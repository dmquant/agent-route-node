"""Hand Protocol — the universal agent execution interface.

Every agent (CLI, HTTP, MCP) implements:
  execute(input, **kwargs) → HandResult
  stream(input, **kwargs)  → AsyncGenerator[str]
  health_check()           → bool

Inspired by Anthropic's Managed Agents: execute(name, input) → string
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, AsyncGenerator, Callable, Any, List
import os
import shutil
import re


# ─── CLI Binary Resolution ──────────────────────────
# Python subprocesses inherit a minimal PATH that may not include
# /opt/homebrew/bin, ~/.nvm, etc. This resolver checks common paths.

_COMMON_BIN_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.nvm/versions/node"),  # nvm — handled specially
]


def resolve_cli_path(binary: str) -> str:
    """Resolve the full path to a CLI binary, with macOS-aware fallbacks.

    1. shutil.which (uses current PATH)
    2. /opt/homebrew/bin (Apple Silicon brew)
    3. /usr/local/bin (Intel brew / standard)
    4. nvm node directories (auto-detect latest version)
    5. Falls back to bare name (let the OS try at spawn time)
    """
    # 1. Standard which
    found = shutil.which(binary)
    if found:
        return found

    # 2-3. Check common bin dirs
    for d in ["/opt/homebrew/bin", "/usr/local/bin"]:
        candidate = os.path.join(d, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # 4. nvm — find latest installed node version
    nvm_base = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_base):
        versions = sorted(os.listdir(nvm_base), reverse=True)
        for v in versions:
            candidate = os.path.join(nvm_base, v, "bin", binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    # 5. Fallback — return bare name and let subprocess raise if missing
    return binary


def get_cli_env() -> dict:
    """Build a subprocess environment with enriched PATH for CLI tools.

    Python venvs and uvicorn often strip PATH to a minimal set.
    This ensures node, npx, git, and brew-installed tools are findable.
    """
    env = os.environ.copy()
    path_parts = env.get("PATH", "").split(os.pathsep)

    # Directories to inject (prepend, in priority order)
    extra_dirs = ["/opt/homebrew/bin", "/usr/local/bin"]

    # nvm — add the latest Node version's bin dir
    nvm_base = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_base):
        versions = sorted(os.listdir(nvm_base), reverse=True)
        for v in versions:
            nvm_bin = os.path.join(nvm_base, v, "bin")
            if os.path.isdir(nvm_bin):
                extra_dirs.insert(0, nvm_bin)
                break

    # Prepend missing dirs
    for d in reversed(extra_dirs):
        if d not in path_parts and os.path.isdir(d):
            path_parts.insert(0, d)

    env["PATH"] = os.pathsep.join(path_parts)
    return env

# ─── Shared Noise Filters ──────────────────────────
# Known CLI boilerplate lines that pollute user-facing output
_NOISE_PATTERNS = [
    # Gemini SDK internals
    re.compile(r'Timeout of \d+ exceeds the interval'),
    re.compile(r"The 'metricReader' option is deprecated"),
    re.compile(r'Loaded cached credentials'),
    re.compile(r'Loading extension:'),
    re.compile(r'Scheduling MCP context refresh'),
    re.compile(r'Executing MCP context refresh'),
    re.compile(r'MCP context refresh complete'),
    re.compile(r'Error executing tool \w+: Tool .* not found'),
    re.compile(r'\[LocalAgentExecutor\] Skipping subagent tool'),
    re.compile(r'\[LocalAgentExecutor\] Blocked call'),
    # Codex startup banner
    re.compile(r'Reading additional input from stdin'),
    re.compile(r'^-+$'),
    re.compile(r'^OpenAI Codex v[\d.]+'),
    re.compile(r'^workdir:'),
    re.compile(r'^model:'),
    re.compile(r'^provider:'),
    re.compile(r'^approval:'),
    re.compile(r'^sandbox:'),
    re.compile(r'^reasoning effort:'),
    re.compile(r'^reasoning summaries:'),
    re.compile(r'^session id:'),
    re.compile(r'codex_core_skills::loader: failed to stat skills entry'),
    # Generic npx noise
    re.compile(r'^npm warn'),
    re.compile(r'^npm notice'),
]


def filter_noise(text: str) -> str:
    """Remove known CLI boilerplate noise while preserving meaningful output."""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append(line)
            continue
        if any(p.search(stripped) for p in _NOISE_PATTERNS):
            continue
        cleaned.append(line)
    result = '\n'.join(cleaned)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def check_rate_limit(output: str) -> Optional[int]:
    """Parse agent output text for rate limit errors and extract wait time in seconds."""
    if not output: return None
    lower_out = output.lower()
    
    if "429 too many requests" not in lower_out and "rate limit" not in lower_out and "quota" not in lower_out:
        return None
        
    sec_match = re.search(r"try again in (\d+)(?:\s*)s", lower_out)
    if sec_match: return int(sec_match.group(1))
    
    min_match = re.search(r"try again in (\d+)(?:\s*)m", lower_out)
    if min_match: return int(min_match.group(1)) * 60
        
    return 3600  # default 1 hour backoff



@dataclass
class HandResult:
    """Universal result from any hand execution."""
    output: str
    exit_code: int
    image_b64: Optional[str] = None
    artifacts: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        d = {"output": self.output, "exitCode": self.exit_code}
        if self.image_b64:
            d["image_b64"] = self.image_b64
        if self.artifacts:
            d["artifacts"] = self.artifacts
        return d


class Hand(ABC):
    """Universal hand interface: execute(name, input) → string.

    Every agent—CLI, HTTP, or MCP—implements this protocol.
    Brains call hands without knowing the transport underneath.
    """

    name: str = "unknown"
    hand_type: str = "unknown"  # "cli" | "http" | "mcp"
    description: str = ""

    @abstractmethod
    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        """Run a task and return structured output.

        Args:
            input: The prompt / task to execute
            workspace_dir: Isolated working directory
            on_log: Callback for streaming log chunks to the UI
            **kwargs: Agent-specific options (e.g. model for Ollama)
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Is this hand available and responsive?"""
        ...

    def info(self) -> dict:
        """Serializable hand metadata."""
        return {
            "name": self.name,
            "type": self.hand_type,
            "description": self.description,
        }

    async def execute_with_retry(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        max_retries: int = 2,
        backoff_base: float = 2.0,
        **kwargs,
    ) -> HandResult:
        """Execute with retry logic for transient failures.

        Retries on: ConnectionError, TimeoutError, OSError.
        Permanent errors (ValueError, RuntimeError) are raised immediately.
        """
        import asyncio

        TRANSIENT = (ConnectionError, TimeoutError, OSError, ConnectionResetError)
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                return await self.execute(input, workspace_dir=workspace_dir, on_log=on_log, **kwargs)
            except TRANSIENT as e:
                last_error = e
                if attempt < max_retries:
                    wait = backoff_base ** attempt
                    if on_log:
                        await on_log(f"\n⚠️ {self.name}: transient error ({e.__class__.__name__}), retrying in {wait:.0f}s (attempt {attempt + 1}/{max_retries})...\n")
                    await asyncio.sleep(wait)
                else:
                    # Final attempt failed
                    return HandResult(
                        output=f"Failed after {max_retries + 1} attempts: {last_error}",
                        exit_code=1,
                    )
            except Exception:
                # Permanent error — don't retry
                raise

        return HandResult(output=f"Failed: {last_error}", exit_code=1)

    def __repr__(self):
        return f"<Hand:{self.name} type={self.hand_type}>"
