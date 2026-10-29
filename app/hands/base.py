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
import codecs
import os
import secrets
import signal
import shutil
import re


# ─── Stream-safe UTF-8 decoding ─────────────────────
# chunk.decode('utf-8', errors='replace') destroys any multi-byte character
# split across a chunk-read boundary — permanently, at read time. CJK is 3
# bytes per char and splits constantly; ASCII never does. That asymmetry
# produced the measured corruption gradient across hands (codex 256-byte
# reads ≈100%, opencode 2048-byte reads ≈29% U+FFFD on CJK-bearing tasks,
# 2026-08-14) and reached canonical KB facts.
#
# An incremental decoder retains the incomplete trailing sequence between
# feeds, so a boundary split never substitutes; only genuinely invalid bytes
# become U+FFFD. At EOF call .decode(b"", final=True) — a dangling partial
# sequence there IS real truncation and is correctly replaced.


def utf8_stream_decoder():
    """Per-stream incremental UTF-8 decoder for subprocess chunk reads."""
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


# ─── CLI Binary Resolution ──────────────────────────
# Python subprocesses inherit a minimal PATH that may not include
# /opt/homebrew/bin, ~/.nvm, etc. This resolver checks common paths.

_COMMON_BIN_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.nvm/versions/node"),  # nvm — handled specially
]


def resolve_cli_path(binary: str) -> str:
    """Resolve the full path to a CLI binary, with cross-platform fallbacks.

    Checks: PATH, brew dirs, nvm, fnm, volta, ~/.local/bin, custom NODE_BIN_DIR
    """
    # 1. Standard which
    found = shutil.which(binary)
    if found:
        return found

    # 2. Check common bin dirs
    search_dirs = [
        "/opt/ros-runtime/npm/bin", "/opt/ros-runtime/node/bin", "/opt/ros-runtime/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.npm-global/bin"),
        # Modern single-binary CLIs install under ~/.<tool>/bin and put it on PATH from the shell
        # profile only, so a non-interactive context -- an ssh command, a daemon started by a
        # script, launchd -- cannot see them. opencode lives at ~/.opencode/bin/opencode on every
        # host here and was not searched: on 2026-09-09 a uvicorn restart from a shell without the
        # profile made every hand unresolvable for twenty minutes while /api/agents still answered
        # 200, because the health endpoint reports the server rather than the tools.
        os.path.expanduser(f"~/.{binary}/bin"),
        os.path.expanduser("~/.bun/bin"),
    ]

    # Custom NODE_BIN from env
    custom = os.getenv("NODE_BIN_DIR")
    if custom:
        search_dirs.insert(0, custom)

    # volta
    volta = os.path.expanduser("~/.volta/bin")
    if os.path.isdir(volta):
        search_dirs.insert(0, volta)

    for d in search_dirs:
        candidate = os.path.join(d, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    # 3. nvm — find latest installed node version
    nvm_base = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_base):
        versions = sorted(os.listdir(nvm_base), reverse=True)
        for v in versions:
            candidate = os.path.join(nvm_base, v, "bin", binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    # 4. fnm
    fnm_base = os.path.expanduser("~/.local/share/fnm/node-versions")
    if os.path.isdir(fnm_base):
        versions = sorted(os.listdir(fnm_base), reverse=True)
        for v in versions:
            candidate = os.path.join(fnm_base, v, "installation", "bin", binary)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate

    # 5. Fallback — return bare name and let subprocess raise if missing
    return binary


# A hand is never the only process it starts.
#
# MEASURED 2026-09-21 on this machine, after rho reported the shape from its own
# host: a wall that calls process.kill() on a launcher kills the LAUNCHER. A
# grandchild kept writing for the full three seconds after the wall fired -- 4
# lines at the kill, 10 afterwards -- while the same test with a new session and
# a group kill stopped it dead at 4 and 4.
#
# That matters here because almost nothing this node runs is a leaf process:
#
#     claude, codex   npx -> node -> the real CLI
#     opencode        an npm .bin shim on the studios
#     any hand        spawns providers and tool processes of its own
#
# So a timed-out task reported as killed can leave a model hand running, still
# holding the workspace and still spending tokens, while the record says the
# task ended at the wall. rho's phrasing is the one to keep: the guard's FAILURE
# reads exactly like the guard never firing.
#
# Two calls, and they must be used together -- a group kill needs a group, and
# start_new_session is what makes one.
def spawn_kwargs() -> dict:
    """Give a spawned hand its own process group, so a wall can reach its children."""
    return {"start_new_session": True}


def kill_process_tree(process, sig=signal.SIGKILL) -> str:
    """Kill the hand AND everything it started. Returns what actually happened.

    Falls back to killing the direct child when there is no group to kill --
    which is the old behaviour, and is still better than raising inside a
    timeout handler.

    A hand started through the launcher (spawn_hand in split mode) runs as
    another user: the node can signal neither it nor its children, and sudo
    does not relay SIGKILL. Such a process carries its run id, and the kill
    goes back through the launcher, which killpg()s the hand's group as the
    hand user.
    """
    run_id = getattr(process, "agentroute_run_id", None)
    if run_id:
        return _kill_via_launcher(process, run_id, sig)
    pid = getattr(process, "pid", None)
    if pid is None:
        return "no pid"
    try:
        os.killpg(os.getpgid(pid), sig)
        return "killed process group %d" % os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError) as e:
        try:
            process.kill()
            return "group kill unavailable (%s); killed pid %d only — " \
                   "CHILDREN MAY SURVIVE" % (type(e).__name__, pid)
        except Exception as e2:
            return "could not kill pid %d: %s" % (pid, e2)


_LAUNCHER_SIGNALS = {signal.SIGKILL: "KILL", signal.SIGTERM: "TERM"}


def _launcher_argv():
    user = os.getenv("AGENT_ROUTE_HAND_USER", "").strip()
    launcher = os.getenv("AGENT_ROUTE_HAND_LAUNCHER", "").strip() or DEFAULT_HAND_LAUNCHER
    return [SUDO_BINARY, "-n", "-u", user, launcher]


_SUDO_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def _kill_via_launcher(process, run_id, sig) -> str:
    import json as _json
    import subprocess as _sp
    def failed(msg):
        # Still reap what we can: sudo is our child, and a wall that leaves the
        # caller awaiting process.wait() forever is worse than a partial kill.
        try:
            process.kill()
        except Exception:
            pass
        return msg + " -- CHILDREN MAY SURVIVE"

    name = _LAUNCHER_SIGNALS.get(sig)
    if name is None:
        return failed("signal %r is not one the launcher sends (TERM/KILL)" % sig)
    if not os.getenv("AGENT_ROUTE_HAND_USER", "").strip():
        return failed("run %s was started through the launcher but AGENT_ROUTE_HAND_USER "
                      "is unset" % run_id)
    line = _json.dumps({"signal": {"run_id": run_id, "sig": name}}) + "\n"
    try:
        r = _sp.run(_launcher_argv(), input=line, capture_output=True, text=True,
                    timeout=15, env=_SUDO_ENV)
    except Exception as e:
        return failed("launcher kill for run %s failed: %s" % (run_id, e))
    if r.returncode != 0:
        return failed("launcher refused kill for run %s: %s" % (
            run_id, (r.stderr or "").strip() or "exit %d" % r.returncode))
    return "via launcher: %s" % (r.stdout or "").strip()


# The node's identity with agent-route and its admin fallback: never a hand's.
NODE_ONLY_CREDENTIALS = ("NODE_KEY", "NODE_TOKEN", "ADMIN_API_KEY")
_NODE_CREDENTIAL_PREFIXES = ("nk_", "dcpn_")


def is_node_credential(value) -> bool:
    """An agent-route node token (nk_) or node key (dcpn_): never a hand's to hold."""
    return isinstance(value, str) and value.strip().startswith(_NODE_CREDENTIAL_PREFIXES)


# ─── The one place a hand process is started ────────
# On the Mac nodes the node and its hands ran as the same macOS user, so any
# hand could read the node's ~/.agent-route/.env (node tokens) no matter what
# get_cli_env stripped from its environment. The user split runs the node as
# the hidden user _agentroute and every hand as the operator, through ONE
# root-owned launcher allowed by a single sudoers line:
#
#     _agentroute ALL=(daoming) NOPASSWD: /usr/local/libexec/agentroute-hand
#
# AGENT_ROUTE_HAND_USER set   -> `sudo -n -u $USER $LAUNCHER`, one JSON line on
#                                stdin ({"hand","argv","cwd","env"}), stdin closed.
#                                The launcher picks the binary from its own
#                                root-owned allowlist and reopens stdin on
#                                /dev/null before exec (scripts/agentroute-hand).
# AGENT_ROUTE_HAND_USER unset -> exactly the old direct spawn, stdin=DEVNULL.
#
# Every hand module spawns through spawn_hand; tests/test_hands_spawn_only_
# through_spawn_hand.py fails, naming the module, if one starts a process itself.
DEFAULT_HAND_LAUNCHER = "/usr/local/libexec/agentroute-hand"
SUDO_BINARY = "/usr/bin/sudo"


def hand_spawn_spec(hand_name: str, cmd: str, args, cwd, env) -> dict:
    """The JSON line the launcher reads. argv[0] is advisory; the launcher replaces it."""
    return {
        "hand": hand_name,
        "argv": [cmd, *[str(a) for a in args]],
        "cwd": cwd or "",
        "env": dict(env) if env else {},
    }


async def spawn_hand(hand_name: str, cmd: str, args, *, cwd=None, env=None,
                     stdout=None, stderr=None, **kwargs):
    """Start a hand subprocess. Returns the asyncio Process.

    `kwargs` (e.g. start_new_session from spawn_kwargs()) pass through to
    asyncio.create_subprocess_exec in both modes.
    """
    import asyncio
    import json as _json

    hand_user = os.getenv("AGENT_ROUTE_HAND_USER", "").strip()
    if not hand_user:
        return await asyncio.create_subprocess_exec(
            cmd, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            cwd=cwd,
            env=env,
            **kwargs,
        )

    launcher = os.getenv("AGENT_ROUTE_HAND_LAUNCHER", "").strip() or DEFAULT_HAND_LAUNCHER
    # The launcher setsid()s the hand and records its pgid under this id; the
    # wall (kill_process_tree) names the run, never a pgid of its own choosing.
    run_id = secrets.token_hex(16)
    spec = hand_spawn_spec(hand_name, cmd, args, cwd, env)
    spec["run_id"] = run_id
    line = _json.dumps(spec) + "\n"
    process = await asyncio.create_subprocess_exec(
        SUDO_BINARY, "-n", "-u", hand_user, launcher,
        stdin=asyncio.subprocess.PIPE,
        stdout=stdout,
        stderr=stderr,
        # sudo resets the environment anyway; the node's own env (and its
        # tokens) is not handed to sudo -- the hand's env travels in the line.
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        **kwargs,
    )
    process.agentroute_run_id = run_id
    try:
        process.stdin.write(line.encode("utf-8"))
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The launcher refused before reading (bad config); its stderr says why.
        pass
    finally:
        process.stdin.close()
    return process


def get_cli_env(cwd: str = "") -> dict:
    """Build a subprocess environment that mirrors a real login shell.

    CLI tools (claude, gemini, codex) authenticate via their own login sessions
    stored in config directories (~/.claude, ~/.config/gemini, etc.).
    The subprocess must have the correct HOME, XDG paths, and full PATH
    so the CLI tools can find their auth tokens.

    Pass `cwd` whenever the subprocess is spawned with `cwd=`. The daemon runs
    from the node repo, so both the login shell below and os.environ carry that
    path in PWD, and asyncio's `cwd=` changes the real working directory
    without touching the variable. Tools that trust PWD over getcwd() then
    resolve their project to the node repo -- measured on opencode: same
    binary, same git-initialised directory, only PWD differing, and
    session.directory follows PWD every time.
    """
    # Start from a login shell's full environment — not the stripped venv env.
    # This ensures CLI auth sessions, PATH, and all config are available.
    try:
        import subprocess as _sp
        shell = os.getenv("SHELL", "/bin/bash")
        result = _sp.run(
            [shell, "-l", "-c", "env"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            env = {}
            for line in result.stdout.split('\n'):
                if '=' in line:
                    key, _, val = line.partition('=')
                    if key:
                        env[key] = val
            # Merge: login shell env as base, current process env overrides
            # (so .env vars from dotenv take precedence)
            for k, v in os.environ.items():
                env[k] = v
        else:
            env = os.environ.copy()
    except Exception:
        env = os.environ.copy()

    # Ensure HOME is correct (critical for CLI auth session lookup)
    home = os.path.expanduser("~")
    env["HOME"] = home
    env.setdefault("USER", os.getenv("USER", ""))
    env.setdefault("SHELL", os.getenv("SHELL", "/bin/bash"))

    # The node's OWN credentials never reach a hand. get_cli_env merges the
    # daemon's environment, which carries the node's .env: measured on gubei12
    # 2026-09-24, all 21 of its keys reached every hand, including NODE_KEY and
    # NODE_TOKEN -- the node's identity with agent-route -- readable by any
    # prompt with `env`. No hand reads them (MCP keys are injected under their
    # own names, even when their value falls back to NODE_TOKEN). The MCP keys
    # themselves stay: `${VAR}` headers resolve from the hand's environment.
    node_values = {v for k in NODE_ONLY_CREDENTIALS if (v := (env.get(k) or os.getenv(k) or "").strip())}
    for k in NODE_ONLY_CREDENTIALS:
        env.pop(k, None)
    # ...and not under ANOTHER name either. ai-institute IKG-274 (2026-10-07): the node's nk_ token
    # also sat in AI_INSTITUTE_MCP_KEY / ROS_GATEWAY_MCP_KEY, so a codex hand ran `env` and printed it.
    # A value that is a node credential, or is shaped like one, is dropped whatever it is called.
    for k in [k for k, v in env.items() if v and (v.strip() in node_values or is_node_credential(v))]:
        env.pop(k, None)

    # A subprocess spawned with cwd= must be told about it. Without this the
    # inherited PWD wins and the tool works in the wrong directory.
    if cwd:
        env["PWD"] = cwd
        env.pop("OLDPWD", None)

    # Ensure XDG dirs are set (some tools use these for config)
    env.setdefault("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    env.setdefault("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    env.setdefault("XDG_CACHE_HOME", os.path.join(home, ".cache"))
    path_parts = env.get("PATH", "").split(os.pathsep)

    # Directories to inject (prepend, in priority order)
    extra_dirs = [
        "/opt/homebrew/bin",   # macOS Apple Silicon brew
        "/usr/local/bin",      # macOS Intel brew / Linux common
        "/usr/bin",            # Linux system
        os.path.expanduser("~/.local/bin"),  # pip/user installs
        os.path.expanduser("~/.npm-global/bin"),  # npm global
    ]

    # nvm — add the latest Node version's bin dir
    nvm_base = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_base):
        versions = sorted(os.listdir(nvm_base), reverse=True)
        for v in versions:
            nvm_bin = os.path.join(nvm_base, v, "bin")
            if os.path.isdir(nvm_bin):
                extra_dirs.insert(0, nvm_bin)
                break

    # fnm (fast node manager)
    fnm_base = os.path.expanduser("~/.local/share/fnm/node-versions")
    if os.path.isdir(fnm_base):
        versions = sorted(os.listdir(fnm_base), reverse=True)
        for v in versions:
            fnm_bin = os.path.join(fnm_base, v, "installation", "bin")
            if os.path.isdir(fnm_bin):
                extra_dirs.insert(0, fnm_bin)
                break

    # volta
    volta_bin = os.path.expanduser("~/.volta/bin")
    if os.path.isdir(volta_bin):
        extra_dirs.insert(0, volta_bin)

    # Custom NODE_BIN from env
    custom_node = os.getenv("NODE_BIN_DIR")
    if custom_node and os.path.isdir(custom_node):
        extra_dirs.insert(0, custom_node)

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
    # The hand already counts tokens and cost off opencode's step_finish events, then renders them
    # into a markdown line -- so every consumer had to read them back out of prose. Neither the
    # institute's queue nor agent-route's task record holds a token or cost column, and on
    # 2026-09-09 both sourced the figure from an operator's paste of this hand's own output string.
    # Carried structurally now. Optional, so hands that do not measure are unaffected.
    metrics: Optional[dict] = None
    # result_mode="final" (local-fabric 73248d53, ai-institute IKG-314): `output` is the hand's final
    # message only, and the full transcript it replaced rides here so the node can keep it and
    # still scan it (rate limits are announced in the transcript, not in the final message).
    transcript: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict:
        d = {"output": self.output, "exitCode": self.exit_code}
        if self.image_b64:
            d["image_b64"] = self.image_b64
        if self.artifacts:
            d["artifacts"] = self.artifacts
        if self.metrics:
            d["metrics"] = self.metrics
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
