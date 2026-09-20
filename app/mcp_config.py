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

from urllib.parse import urlparse

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
        # Each server names its OWN env var. Two servers sharing one would put
        # the second's credential in the first's variable, which is how a node
        # ends up presenting a credential that is not the one its config names
        # -- the shape that cost this federation an evening on 2026-09-20.
        "key_var": _KEY_VAR,
        # Named so a caller can redirect THIS server without guessing which
        # variable controls it -- the install gate points each configured
        # server at its own proxy, and mapping by name would break the moment
        # AI_INSTITUTE_MCP_NAME is set.
        "url_var": "AI_INSTITUTE_MCP_URL",
        "hands": hands,
    }


# THE SECOND GOVERNED SERVER.
#
# ros-gateway fronts the same federation (institute, rho, epistates) and
# authenticates DIFFERENTLY, which is the whole reason it matters here:
#
#   measured 2026-09-20, four arms one variable apart, on tools/CALL --
#     Authorization: Bearer alone   -> HTTP 200
#     X-Institute-Token alone       -> HTTP 401 "no bearer token"
#     no auth                       -> HTTP 401
#
# Bearer is the ONLY credential form codex can express, and the institute
# rejects Bearer. So the hand this module has had to decline since 2026-09-11
# is exactly the hand this endpoint is reachable by, and governed coverage goes
# from 3 of 4 to 4 of 4 through it.
#
# The header is NOT read from the environment. Every other knob here is, and
# that is right for a value that might change; this one was measured and an
# operator who "corrects" it to X-Institute-Token silently disables codex again.
_GATEWAY_KEY_VAR = "ROS_GATEWAY_MCP_KEY"


def gateway_settings() -> Optional[dict]:
    """The ros-gateway server, or None when it is not configured on this node."""
    url = (os.getenv("ROS_GATEWAY_MCP_URL") or "").strip()
    if not url:
        return None
    key = (os.getenv(_GATEWAY_KEY_VAR) or os.getenv("NODE_TOKEN") or "").strip()
    if not key:
        _logger.warning("ROS_GATEWAY_MCP_URL is set but no credential is available "
                        "— the gateway server is not configured")
        return None
    hands = [h.strip() for h in
             (os.getenv("ROS_GATEWAY_MCP_HANDS") or "opencode,claude,codex,agy").split(",")
             if h.strip()]
    return {"url": url, "key": key,
            "name": (os.getenv("ROS_GATEWAY_MCP_NAME") or "ros-gateway").strip(),
            "header": "Authorization", "scheme": "Bearer ",
            "key_var": _GATEWAY_KEY_VAR, "url_var": "ROS_GATEWAY_MCP_URL",
            "hands": hands}


def servers_for(hand: str) -> list:
    """Every governed server this hand should be given, in a stable order.

    Two servers rather than one is the point: they authenticate differently and
    reach overlapping estates, so a hand excluded from one may still be served
    by the other. Ordered institute-first so a hand that can take only some of
    them takes the primary one.
    """
    out = []
    for cfg in (settings(), gateway_settings()):
        if cfg and hand in cfg["hands"]:
            out.append(cfg)
    return out


def preflight(hand: str, timeout: float = 6.0) -> dict:
    """Which governed servers actually answer, right now, before the hand runs.

    WHY A NODE SHOULD DO THIS AT ALL.

    Measured 2026-09-21: a hand fabricates a complete, correct-looking tool
    result under exactly one condition -- a tool that is SILENTLY ABSENT plus a
    prompt that describes the expected shape. Either alone is safe. And a tool
    description necessarily describes a shape, so the removable half is the
    silence.

    The federation was manufacturing that cell in production the same day: one
    namespace was unreachable all day, its tools were omitted from the listing
    without comment, and every hand saw a smaller surface with nothing saying
    so. This node does the same thing -- it writes a config and never checks
    the server came up.

    Uses tools/list, which needs no auth on these endpoints. That makes it a
    REACHABILITY probe and NOT an authorisation one, and this function says so
    in its own result rather than in a comment, because a caller that reads
    `reachable` as `usable` is the next defect.
    """
    import urllib.request
    import urllib.error

    out = {"checked": [], "reachable": [], "unreachable": [],
           "proves": "reachability only — tools/list needs no auth here, so a "
                     "reachable server may still refuse every tools/call"}
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/list", "params": {}}).encode()
    for cfg in servers_for(hand):
        out["checked"].append(cfg["name"])
        req = urllib.request.Request(cfg["url"], data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            cfg["header"]: cfg["scheme"] + cfg["key"]})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ok = r.status == 200
                r.read(1)
        except Exception as e:
            ok, err = False, "%s: %s" % (type(e).__name__, e)
        if ok:
            out["reachable"].append(cfg["name"])
        else:
            out["unreachable"].append({"name": cfg["name"], "why": err[:120]})
    return out


def reachability_notice(pre: dict) -> str:
    """The line a hand should be told when a governed server did not come up.

    ⚠ THIS IS THE WEAKER OF THE TWO CHANNELS, BY THIS ESTATE'S OWN MEASUREMENT.

    It removes the silence by putting a sentence in the PROMPT -- which is
    prose to a model, the channel where every defect found on 2026-09-20/21 was
    located, and the one measured inert on at least one hand: codex was told
    outright "the result contains an _absence record; read it before
    answering", and answered as if it had not been.

    So a caller must not read this as making an unavailable server safe. It
    makes the absence STATED, which is strictly better than silent, and it
    relies on the hand reading it, which one hand demonstrably does not.

    THE STRONGER FORM, for anyone building it: give the unavailable namespace a
    stub that REGISTERS its tools and fails every call explicitly. The hand
    then sees the tool, calls it, and receives a loud error -- which is the
    "explicit error" cell of the fabrication grid, and that cell came back
    honest on every hand tested, including the one that fabricated.

    Empty when everything answered: a notice on every task is a notice nobody
    reads.
    """
    if not pre.get("unreachable"):
        return ""
    names = ", ".join(u["name"] for u in pre["unreachable"])
    return ("MCP SERVERS UNAVAILABLE FOR THIS TASK: %s. Tools from %s are NOT in "
            "your toolset right now. This is a statement of fact, not a listing "
            "gap: if a task needs them, say so in your output rather than "
            "reconstructing what they would have returned."
            % (names, "it" if len(pre["unreachable"]) == 1 else "them"))


def _mcp_dir() -> str:
    """Per-task CLI config lives beside the node's data, never inside the
    workspace — the workspace is uploaded to R2 after every task."""
    d = os.path.join(get_data_dir(), "mcp")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _headers(cfg: dict, ref: str) -> dict:
    return {cfg["header"]: cfg["scheme"] + ref}


def _env_for(servers: list) -> dict:
    """Each server's credential under its OWN variable name."""
    return {c["key_var"]: c["key"] for c in servers}


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


def _prepare_opencode(servers: list, workspace_dir: str) -> McpPlan:
    """opencode reads opencode.json from its PROJECT directory, which is the
    workspace (verified: a project-local file shows up in `opencode mcp list`).
    Merge rather than overwrite — a task may legitimately ship its own config."""
    path = os.path.join(workspace_dir, "opencode.json")
    doc = _load_json_or_empty(path)
    if doc is None:
        # Do not clobber something we failed to parse.
        return McpPlan(note="opencode.json present but unparseable — MCP not configured")
    doc.setdefault("$schema", "https://opencode.ai/config.json")
    mcp = doc.setdefault("mcp", {})
    for cfg in servers:
        mcp[cfg["name"]] = {
            "type": "remote",
            "url": cfg["url"],
            "enabled": True,
            "headers": _headers(cfg, "{env:%s}" % cfg["key_var"]),
        }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    return McpPlan(env=_env_for(servers),
                   note="opencode.json <- mcp.%s" % ", mcp.".join(c["name"] for c in servers))


def _prepare_claude(servers: list, workspace_dir: str) -> McpPlan:
    """claude takes --mcp-config with a path, so the file goes outside the
    workspace and never reaches R2. --strict-mcp-config stops the task also
    inheriting whatever MCP servers the host happens to have configured, which
    would make one node's behaviour differ from another's."""
    name = os.path.basename(workspace_dir.rstrip("/")) or "task"
    path = os.path.join(_mcp_dir(), f"{name}.json")
    doc = {"mcpServers": {c["name"]: {
        "type": "http",
        "url": c["url"],
        "headers": _headers(c, "${%s}" % c["key_var"]),
    } for c in servers}}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return McpPlan(args=["--mcp-config", path, "--strict-mcp-config"],
                   env=_env_for(servers),
                   note="--mcp-config %s (%s)" % (path, ", ".join(c["name"] for c in servers)))


def _prepare_codex(servers: list, workspace_dir: str) -> McpPlan:
    """codex takes `-c` dotted overrides parsed as TOML, so the whole server can
    be declared on the command line with nothing written to disk at all.

    Its only auth mechanism for a streamable-HTTP server is
    `bearer_token_env_var` -- it cannot express any other header.

    THIS USED TO DECLINE OUTRIGHT, AND THAT WAS RIGHT WHILE THERE WAS ONE
    SERVER. The institute rejects Bearer on tools/call (401, category auth,
    measured three arms), so configuring codex for it would have handed a model
    a server that 401s inside its tool loop -- the expensive failure, because a
    model cannot tell an auth error from a missing file and starts hunting.

    With a second governed server the question stops being about the HAND and
    becomes about each SERVER. ros-gateway requires Bearer -- measured four arms
    on tools/call, Authorization alone returns 200 -- so codex can reach it.
    Declining every server because one is unreachable would keep codex at zero
    tools when 73 are available to it.

    So: configure what this hand can express, decline what it cannot, and say
    which in the note. An empty result still declines, with the reason.
    """
    usable, declined = [], []
    for cfg in servers:
        if cfg["header"].lower() == "authorization" and cfg["scheme"] == "Bearer ":
            usable.append(cfg)
        else:
            declined.append("%s (needs %s)" % (cfg["name"], cfg["scheme"] + cfg["header"]))

    args = []
    for cfg in usable:
        n = cfg["name"]
        args += [
            "-c", f'mcp_servers.{n}.url="{cfg["url"]}"',
            "-c", f'mcp_servers.{n}.bearer_token_env_var="{cfg["key_var"]}"',
            # Bound the wait so an MCP that is down costs seconds, not the task.
            "-c", f"mcp_servers.{n}.startup_timeout_sec=10",
        ]

    note = ""
    if usable:
        note = "-c mcp_servers.%s.*" % ".*, mcp_servers.".join(c["name"] for c in usable)
    if declined:
        note = (note + "; " if note else "") + \
               "codex supports only Authorization: Bearer — declined " + ", ".join(declined)
    return McpPlan(args=args, env=_env_for(usable), note=note)


_AGY_CONFIG = "~/.gemini/config/mcp_config.json"


def _prepare_agy(servers: list, workspace_dir: str) -> McpPlan:
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
    # REFUSE TO WRITE A PRIVATE ADDRESS INTO A NODE-GLOBAL FILE.
    #
    # agy's registry is the only configuration this module writes that OUTLIVES
    # the task, so a test URL landing in it is a production break rather than a
    # stale temp file. That happened TWICE on 2026-09-20/21, both times because
    # an ad-hoc script called prepare() outside the verify gate's
    # NodeGlobalConfig guard: once leaving a ${VAR} the hand cannot expand, once
    # leaving a proxy port that no longer existed. Each time the node looked
    # configured and reached nothing.
    #
    # Documenting it did not stop the second one, so it is a refusal now. The
    # gate sets AI_INSTITUTE_MCP_ALLOW_LAN because it holds the guard that puts
    # the file back; anything else gets a message naming both the reason and the
    # way through, rather than a silent success.
    if not (os.getenv("AI_INSTITUTE_MCP_ALLOW_LAN") or "").strip():
        for cfg in servers:
            host = urlparse(cfg["url"]).hostname or ""
            if host in ("localhost",) or host.startswith(("127.", "10.", "192.168.")) \
                    or (host.startswith("172.") and host.split(".")[1:2]
                        and 16 <= int(host.split(".")[1] or 0) <= 31):
                return McpPlan(note=(
                    "refusing to write %s -> %s into the NODE-GLOBAL agy registry: "
                    "a private address here outlives the task and has twice left this "
                    "node pointing at something that no longer exists. Set "
                    "AI_INSTITUTE_MCP_ALLOW_LAN=1 if you hold a snapshot/restore guard."
                    % (cfg["name"], cfg["url"])))

    path = os.path.expanduser(_AGY_CONFIG)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    doc = _load_json_or_empty(path)
    if doc is None:
        return McpPlan(note=f"{_AGY_CONFIG} present but unparseable — MCP not configured")
    literal = (os.getenv("AI_INSTITUTE_MCP_AGY_LITERAL") or "").lower() in ("1", "true", "yes")
    registry = doc.setdefault("mcpServers", {})
    wanted = {}
    for cfg in servers:
        ref = cfg["key"] if literal else "${%s}" % cfg["key_var"]
        wanted[cfg["name"]] = {"disabled": False, "serverUrl": cfg["url"],
                               "headers": _headers(cfg, ref)}

    # A LITERAL here is what made the 2026-09-20 transposition possible: two
    # nodes held each other's token because the value, not a reference, sat in
    # this file and the file travels between machines. An env reference is
    # per-machine by construction -- the same file presents a different
    # identity on each host -- so the escape hatch stays but says so.
    if literal:
        _logger.warning("AI_INSTITUTE_MCP_AGY_LITERAL writes credential VALUES into %s. "
                        "That file is node-global and has been observed copied between "
                        "hosts; a literal can therefore end up presenting another "
                        "machine's identity.", _AGY_CONFIG)

    if all(registry.get(n) == e for n, e in wanted.items()):
        # Already correct. Say so rather than rewriting the file on every task.
        return McpPlan(env=_env_for(servers), note="agy registry already current")
    registry.update(wanted)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return McpPlan(env=_env_for(servers),
                   note="%s <- %s (node-global%s)" % (_AGY_CONFIG, ", ".join(wanted),
                                                      ", literal key" if literal else ""))


def remove_agy_entry() -> str:
    """Undo _prepare_agy. The agy registry is node-global, so turning the feature
    off in .env is not enough to stop agy offering the server — this is how the
    entry actually goes away."""
    path = os.path.expanduser(_AGY_CONFIG)
    if not os.path.exists(path):
        return "no agy registry"
    name = (os.getenv("AI_INSTITUTE_MCP_NAME") or "ai-institute").strip()
    # Same tolerance as the install path, and for the same measured file.
    #
    # _load_json_or_empty exists because a zero-byte registry is real -- gubei12
    # has carried one since May, studio01 since 2026-09-15 -- and a bare
    # json.load raises on it. The INSTALL path was hardened and this one was
    # not, so the asymmetry was: turning the feature on tolerated the file and
    # turning it OFF raised. That is the wrong way round. This is the
    # revocation path, and a revocation that throws leaves the entry in place
    # while reading as an error someone may retry, ignore, or misdiagnose.
    doc = _load_json_or_empty(path)
    if doc is None:
        return f"agy registry at {_AGY_CONFIG} is malformed; refusing to rewrite it"
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
    servers = servers_for(hand)
    if not servers:
        if not settings() and not gateway_settings():
            return McpPlan()                      # feature off entirely
        return McpPlan(note=f"{hand} is in no server's configured hand set")
    writer = _WRITERS.get(hand)
    if not writer:
        return McpPlan(note=f"{hand} has no per-task MCP surface yet (stage 2)")
    try:
        plan = writer(servers, workspace_dir)
    except Exception as e:
        _logger.warning("MCP setup failed for %s: %s — falling back to the R2 path", hand, e)
        return McpPlan(note=f"setup failed: {e}")
    if plan.note:
        _logger.info("MCP %s: %s", hand, plan.note)
    return plan
