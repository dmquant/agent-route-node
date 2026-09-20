"""
MCP file delivery (stage 1) — a second, parallel path for getting a task its files.

The node has always pre-staged a session's files into the workspace, pulling them
from the worker's R2 store before a hand runs (`_sync_workspace_from_r2` in
task_puller). **That path is untouched and still runs.** This module adds a
different one beside it: configure the hand's CLI with a remote MCP server so the
model fetches the files the prompt names, at the moment it needs them, instead of
receiving them inlined in the prompt.

Why a second path rather than a replacement: the two fail differently and we do
not yet know which failure is cheaper. Running them together is how we find out
without betting the fleet on the answer.

INERT UNLESS CONFIGURED. With AI_INSTITUTE_MCP_URL unset, prepare() returns an
empty plan and every hand spawns byte-identically to before. That is the whole
safety story for stage 1 — there is no code path that behaves differently on an
unconfigured node.

Environment:
    AI_INSTITUTE_MCP_URL     base URL of the MCP server. Empty/unset = feature off.
    AI_INSTITUTE_MCP_KEY     credential. Defaults to NODE_TOKEN.
    AI_INSTITUTE_MCP_HEADER  header carrying it. Default "Authorization".
    AI_INSTITUTE_MCP_SCHEME  value prefix. Default "Bearer " for Authorization,
                             empty for anything else.
    AI_INSTITUTE_MCP_NAME    server name the model sees. Default "ai-institute".
    AI_INSTITUTE_MCP_HANDS   comma list of hands to configure. Default "opencode,claude,codex,agy".

Stage 1 covers opencode, claude and codex — the hands with a genuinely per-task
configuration surface, each verified on a real node: a project-local opencode.json
shows up in `opencode mcp list`; claude takes --mcp-config; codex takes -c dotted
overrides, and a deliberately unreachable URL logged a transport error and let the
task finish, which is the fallback behaviour this needs.

agy is covered too, but differently: it has no per-task scope at all, so its entry
is node-global. See _prepare_agy.

Not covered: gemini. It is obsolete — agy replaced it — and it has drawn zero of
the last 500 fleet tasks. It gets an empty plan and a note.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from app.config import get_data_dir

_logger = logging.getLogger("mcp_config")

# The credential never reaches disk as a literal. Both CLIs expand an env
# reference out of the child's environment, so the config file carries the
# NAME of the variable and the spawn carries its value.
_KEY_VAR = "AI_INSTITUTE_MCP_KEY"

# THE header default, for every module that reads AI_INSTITUTE_MCP_HEADER.
#
# It lives here alone because it did not, and the drift shipped. This module
# defaulted to "Authorization" while institute_files defaulted the SAME variable
# to "X-Admin-Token", so a node that set the URL without setting the header got
# hands configured with Bearer -- 401 inside the model's tool loop -- while the
# file-fetch path on that same node authenticated correctly. One node was safe
# only because the variable happened to be set there by hand.
#
# The value is the measured one, not the convenient one. Measured 2026-09-11: an
# institute nk_ token authenticates tools/call ONLY as X-Admin-Token; X-API-Key
# and Authorization: Bearer both return 401. tools/list needs no auth at all, so
# a 200 there proves nothing -- reading one as proof is the scar behind this.
DEFAULT_HEADER = "X-Admin-Token"

_warned_default_header = False


def _resolve_header() -> str:
    """The header, and a trace when it was defaulted rather than chosen.

    A default that substitutes silently manufactures a state indistinguishable
    from a deliberate one: a node nobody configured looks exactly like a node
    configured correctly, and stays that way. The failing case is not the value
    being wrong -- it is right -- but that when the institute next changes what
    it accepts, the unconfigured nodes are unfindable.

    So the substitution still happens (refusing would take down working nodes
    over a correct value) and it says so once per process. Log line, not an
    exception: the bound on this is that a sane default may stand, provided it
    leaves a trace.
    """
    global _warned_default_header
    chosen = (os.getenv("AI_INSTITUTE_MCP_HEADER") or "").strip()
    if chosen:
        return chosen
    if not _warned_default_header:
        _warned_default_header = True
        _logger.warning(
            "AI_INSTITUTE_MCP_HEADER is unset — defaulting to %r. This node is "
            "indistinguishable from one configured deliberately; set it explicitly "
            "in the data-dir .env so a future auth change can find it.",
            DEFAULT_HEADER)
    return DEFAULT_HEADER

SUPPORTED_HANDS = ("opencode", "claude", "codex", "agy")


@dataclass
class McpPlan:
    """What a hand must add to its spawn. Empty when the feature is off."""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.args or self.env)


def settings() -> Optional[dict]:
    """Resolved configuration, or None when the feature is off."""
    url = (os.getenv("AI_INSTITUTE_MCP_URL") or "").strip()
    if not url:
        return None
    key = (os.getenv(_KEY_VAR) or os.getenv("NODE_TOKEN") or "").strip()
    if not key:
        # A URL with no credential would produce 401s inside the model's tool
        # loop, which is the expensive failure mode: the model cannot tell an
        # auth error from a missing file and starts hunting. Refuse instead.
        _logger.warning("AI_INSTITUTE_MCP_URL is set but no credential is available — MCP disabled")
        return None
    header = _resolve_header()
    scheme = os.getenv("AI_INSTITUTE_MCP_SCHEME")
    if scheme is None:
        scheme = "Bearer " if header.lower() == "authorization" else ""
    hands = [h.strip() for h in (os.getenv("AI_INSTITUTE_MCP_HANDS") or "opencode,claude,codex,agy").split(",") if h.strip()]
    return {
        "url": url,
        "key": key,
        "name": (os.getenv("AI_INSTITUTE_MCP_NAME") or "ai-institute").strip(),
        "header": header,
        "scheme": scheme,
        "hands": hands,
    }


def _mcp_dir() -> str:
    """Per-task CLI config lives beside the node's data, never inside the
    workspace — the workspace is uploaded to R2 after every task."""
    d = os.path.join(get_data_dir(), "mcp")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _headers(cfg: dict, ref: str) -> dict:
    return {cfg["header"]: cfg["scheme"] + ref}


def _load_json_or_empty(path: str):
    """Existing JSON document, {} when absent or blank, None when it is really
    malformed.

    The blank case is not hypothetical: gubei12 has carried a zero-byte
    ~/.gemini/config/mcp_config.json since May. json.load raises on it, and an
    earlier version of this module read that as "unreadable" and quietly declined
    to configure the hand — the feature would have looked switched on and done
    nothing on the one node that runs agy most.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        return (json.loads(text) or {}) if text else {}
    except Exception:
        return None


def _prepare_opencode(cfg: dict, workspace_dir: str) -> McpPlan:
    """opencode reads opencode.json from its PROJECT directory, which is the
    workspace (verified: a project-local file shows up in `opencode mcp list`).
    Merge rather than overwrite — a task may legitimately ship its own config."""
    path = os.path.join(workspace_dir, "opencode.json")
    doc = _load_json_or_empty(path)
    if doc is None:
        # Do not clobber something we failed to parse.
        return McpPlan(note="opencode.json present but unparseable — MCP not configured")
    doc.setdefault("$schema", "https://opencode.ai/config.json")
    doc.setdefault("mcp", {})[cfg["name"]] = {
        "type": "remote",
        "url": cfg["url"],
        "enabled": True,
        "headers": _headers(cfg, "{env:%s}" % _KEY_VAR),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return McpPlan(env={_KEY_VAR: cfg["key"]}, note=f"opencode.json <- mcp.{cfg['name']}")


def _prepare_claude(cfg: dict, workspace_dir: str) -> McpPlan:
    """claude takes --mcp-config with a path, so the file goes outside the
    workspace and never reaches R2. --strict-mcp-config stops the task also
    inheriting whatever MCP servers the host happens to have configured, which
    would make one node's behaviour differ from another's."""
    name = os.path.basename(workspace_dir.rstrip("/")) or "task"
    path = os.path.join(_mcp_dir(), f"{name}.json")
    doc = {"mcpServers": {cfg["name"]: {
        "type": "http",
        "url": cfg["url"],
        "headers": _headers(cfg, "${%s}" % _KEY_VAR),
    }}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return McpPlan(args=["--mcp-config", path, "--strict-mcp-config"],
                   env={_KEY_VAR: cfg["key"]},
                   note=f"--mcp-config {path}")


def _prepare_codex(cfg: dict, workspace_dir: str) -> McpPlan:
    """codex takes `-c` dotted overrides parsed as TOML, so the whole server can
    be declared on the command line with nothing written to disk at all.

    Its only auth mechanism for a streamable-HTTP server is
    `bearer_token_env_var`. If the institute wants the credential in some other
    header, codex cannot express it — say so and stay out rather than configure
    a server that will 401 inside the model's tool loop."""
    if cfg["header"].lower() != "authorization" or cfg["scheme"] != "Bearer ":
        return McpPlan(note="codex supports only Authorization: Bearer — header %r not expressible"
                            % (cfg["scheme"] + cfg["header"]))
    n = cfg["name"]
    args = [
        "-c", f'mcp_servers.{n}.url="{cfg["url"]}"',
        "-c", f'mcp_servers.{n}.bearer_token_env_var="{_KEY_VAR}"',
        # Bound the wait so an MCP that is down costs seconds, not the task.
        "-c", f"mcp_servers.{n}.startup_timeout_sec=10",
    ]
    return McpPlan(args=args, env={_KEY_VAR: cfg["key"]}, note=f"-c mcp_servers.{n}.*")


_AGY_CONFIG = "~/.gemini/config/mcp_config.json"


def _prepare_agy(cfg: dict, workspace_dir: str) -> McpPlan:
    """agy keeps MCP servers in one global registry at ~/.gemini/config/mcp_config.json
    — it is a gemini-CLI descendant, which is what it replaced.

    There is no per-task scope. Measured on gubei12: a workspace-local
    .gemini/config/mcp_config.json is ignored even when agy runs from that
    workspace, and none of GEMINI_CONFIG_DIR, GEMINI_DIR, AGY_HOME,
    AGY_CONFIG_DIR or XDG_CONFIG_HOME redirects the lookup. Overriding HOME does
    work, but HOME is also where agy keeps its credentials, so that trade is not
    available.

    So this entry is node-global rather than per-task. It is written idempotently
    and touches only our own server name, leaving any other server alone.

    The credential: `agy mcp add` creates this file 0644 — world-readable — so the
    token must not be written literally. The header carries an env reference and
    the spawn carries the value, and the file is forced to 0600 regardless.
    Whether agy expands ${VAR} in a header at connect time is the one thing here
    that could not be verified without a live endpoint; if the smoke test comes
    back 401, set AI_INSTITUTE_MCP_AGY_LITERAL=1 to write the token itself
    instead, still at 0600.
    """
    path = os.path.expanduser(_AGY_CONFIG)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    doc = _load_json_or_empty(path)
    if doc is None:
        return McpPlan(note=f"{_AGY_CONFIG} present but unparseable — MCP not configured")
    literal = (os.getenv("AI_INSTITUTE_MCP_AGY_LITERAL") or "").lower() in ("1", "true", "yes")
    ref = cfg["key"] if literal else "${%s}" % _KEY_VAR
    entry = {"disabled": False, "serverUrl": cfg["url"], "headers": _headers(cfg, ref)}
    servers = doc.setdefault("mcpServers", {})
    if servers.get(cfg["name"]) == entry:
        # Already correct. Say so rather than rewriting the file on every task.
        return McpPlan(env={_KEY_VAR: cfg["key"]}, note="agy registry already current")
    servers[cfg["name"]] = entry
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return McpPlan(env={_KEY_VAR: cfg["key"]},
                   note="%s <- %s (node-global%s)" % (_AGY_CONFIG, cfg["name"],
                                                      ", literal key" if literal else ""))


def remove_agy_entry() -> str:
    """Undo _prepare_agy. The agy registry is node-global, so turning the feature
    off in .env is not enough to stop agy offering the server — this is how the
    entry actually goes away."""
    path = os.path.expanduser(_AGY_CONFIG)
    if not os.path.exists(path):
        return "no agy registry"
    name = (os.getenv("AI_INSTITUTE_MCP_NAME") or "ai-institute").strip()
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh) or {}
    if name not in (doc.get("mcpServers") or {}):
        return f"{name} not in agy registry"
    del doc["mcpServers"][name]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return f"removed {name} from {_AGY_CONFIG}"


_WRITERS = {"opencode": _prepare_opencode, "claude": _prepare_claude,
            "codex": _prepare_codex, "agy": _prepare_agy}


def prepare(hand: str, workspace_dir: str) -> McpPlan:
    """Configure `hand` to reach the institute MCP for this one task.

    Returns an empty plan when the feature is off, when this hand is not in the
    configured set, or when writing the configuration fails. Never raises: a
    task that cannot reach the MCP must still run on the old path, which is
    still delivering its files.
    """
    cfg = settings()
    if not cfg:
        return McpPlan()
    if hand not in cfg["hands"]:
        return McpPlan(note=f"{hand} not in AI_INSTITUTE_MCP_HANDS")
    writer = _WRITERS.get(hand)
    if not writer:
        return McpPlan(note=f"{hand} has no per-task MCP surface yet (stage 2)")
    try:
        plan = writer(cfg, workspace_dir)
    except Exception as e:
        _logger.warning("MCP setup failed for %s: %s — falling back to the R2 path", hand, e)
        return McpPlan(note=f"setup failed: {e}")
    if plan.note:
        _logger.info("MCP %s: %s", hand, plan.note)
    return plan
