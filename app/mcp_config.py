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
    AI_INSTITUTE_MCP_HANDS   comma list of hands to configure. Default "opencode,claude".

Stage 1 covers opencode and claude — the two hands the smoke test exercises, and
the two with a genuinely per-task configuration surface (a project-local
opencode.json, and claude's --mcp-config flag). codex, gemini and agy are stage 2:
codex takes -c dotted overrides and gemini a project .gemini/settings.json, both
plausible and neither verified yet; agy exposes only a global `agy mcp` registry,
which is not per-task and needs a different answer. A hand outside the covered set
gets an empty plan and a note, never a half-configuration.
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

SUPPORTED_HANDS = ("opencode", "claude")


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
    header = (os.getenv("AI_INSTITUTE_MCP_HEADER") or "Authorization").strip()
    scheme = os.getenv("AI_INSTITUTE_MCP_SCHEME")
    if scheme is None:
        scheme = "Bearer " if header.lower() == "authorization" else ""
    hands = [h.strip() for h in (os.getenv("AI_INSTITUTE_MCP_HANDS") or "opencode,claude").split(",") if h.strip()]
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


def _prepare_opencode(cfg: dict, workspace_dir: str) -> McpPlan:
    """opencode reads opencode.json from its PROJECT directory, which is the
    workspace (verified: a project-local file shows up in `opencode mcp list`).
    Merge rather than overwrite — a task may legitimately ship its own config."""
    path = os.path.join(workspace_dir, "opencode.json")
    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh) or {}
        except Exception as e:
            # Do not clobber something we failed to parse.
            return McpPlan(note=f"opencode.json present but unreadable ({e}) — MCP not configured")
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


_WRITERS = {"opencode": _prepare_opencode, "claude": _prepare_claude}


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
