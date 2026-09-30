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
import hashlib
import os
import urllib.error
import urllib.request
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
# and Authorization: Bearer both return 401.
#
# THE NEXT SENTENCE USED TO READ "tools/list needs no auth at all, so a 200 there
# proves nothing". THAT IS NO LONGER TRUE, and it stayed here after this same file
# recorded the measurement that overturned it (see _probe, 2026-09-22). Re-measured
# 2026-09-23 from sg01 AND gubei12, three credential arms on both endpoints:
#
#     ai-institute   real key 200/57 tools · zeroed 401 · absent 401
#     ros-gateway    real key 200/92 tools · zeroed 401 · absent 401
#
# So tools/list IS gated on both, and a 200 there DOES prove the credential was
# accepted. rho RHO-172 named this shape the same hour, in their own estate: one
# writer, one field, and a reader still holding its superseded meaning. Here the
# reader was preflight(), which published the stale sentence as a caveat to every
# caller -- so the correction belongs in the code that ASKS, not only in a comment.
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

    `proves` USED TO BE A CONSTANT STRING, AND IT WAS WRONG.

    It read "reachability only — tools/list needs no auth here", which this
    estate's own _probe had already contradicted in this same file. A claim that
    is a literal in the result is not a finding about the endpoint; it is a
    finding about whoever typed it, and it cannot be cleared by fixing what it
    describes. This estate's rule for a check applies to a caveat too.

    So the gating is now MEASURED, one extra request per server: the same
    tools/list with the credential ABSENT. Present-and-wrong is a third arm and
    is deliberately NOT run here -- absent is the arm that answers "is this
    endpoint gated at all", and verify_server already runs the wrong-credential
    arm for the question preflight is not asking.

    Per server the result carries `gated`:

        True     the no-credential arm was refused, so a 200 with the credential
                 means the credential was ACCEPTED, not merely that a port answered
        False    the endpoint served tools/list to nobody in particular; a 200
                 with the credential proves reachability and nothing more
        None     the control itself could not run (timeout, DNS, 5xx). THAT IS
                 NOT A False. An unmeasured gate must not read as an open one.

    And the direction matters, which is why this is worth a request: the failure
    of the old constant was toward UNDERSTATEMENT -- it told callers a 200 proved
    less than it did, so a caller who needed the stronger fact had to build a
    second probe by hand. Three were built by hand on 2026-09-23 and two of them
    were wrong in the way 137e772 had already fixed inside this module. A stale
    caveat does not just misinform; it manufactures the duplicate instrument that
    carries the next defect.

    NO CALLER TODAY. preflight() and reachability_notice() are reached from
    nothing in app/, scripts/ or tests/ -- verify_server, which prepare() does
    call, supersedes the credential question. That is said here rather than left
    to be rediscovered, because a function with a published claim and no caller
    is how f17b320 happened one commit ago.
    """
    import urllib.request
    import urllib.error

    def _list(cfg, with_credential):
        hdrs = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                PROBE_HEADER: "agent-route-node/preflight",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        if with_credential:
            hdrs[cfg["header"]] = cfg["scheme"] + cfg["key"]
        req = urllib.request.Request(cfg["url"], data=body, method="POST", headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(1)
                return r.status, None
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, e)

    out = {"checked": [], "reachable": [], "unreachable": [], "gated": {},
           "proves": None}
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "tools/list", "params": {}}).encode()
    for cfg in servers_for(hand):
        out["checked"].append(cfg["name"])
        status, err = _list(cfg, True)
        # THE CONTROL: the same call with no credential at all.
        ctl_status, ctl_err = _list(cfg, False)
        if ctl_err is not None or ctl_status is None:
            gated = None
        else:
            gated = ctl_status in (401, 403)
        out["gated"][cfg["name"]] = gated
        if status == 200:
            out["reachable"].append(cfg["name"])
        else:
            out["unreachable"].append({
                "name": cfg["name"],
                "why": (err or "HTTP %s" % status)[:120]})
    measured = [v for v in out["gated"].values()]
    if not measured:
        out["proves"] = "nothing — no governed server is configured for this hand"
    elif all(v is True for v in measured):
        out["proves"] = ("credential acceptance — the no-credential control was "
                         "refused on every server checked, so a reachable server "
                         "here also accepted this node's credential")
    elif any(v is None for v in measured):
        out["proves"] = ("reachability, and the gating is UNMEASURED on at least "
                         "one server — the no-credential control could not run, "
                         "which is not the same as an open endpoint")
    else:
        out["proves"] = ("reachability only on at least one server — its "
                         "tools/list answered without a credential, so a 200 "
                         "there says a port answered and nothing about auth")
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


# The agy registry spells a server's endpoint under any of three keys, and the
# two nodes in this estate disagree TODAY: gubei12 carries `serverUrl`, sg01
# carries `url` beside a `type` this writer does not model. Both reach the
# gateway; both are working configurations.
_URL_KEYS = ("serverUrl", "httpUrl", "url")


def _entry_url(entry: dict) -> str:
    """The endpoint an existing registry entry points at, whatever it calls it."""
    if not isinstance(entry, dict):
        return ""
    for k in _URL_KEYS:
        v = entry.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


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
    MEASURED 2026-09-20, and the answer is the opposite of what this docstring
    used to say: agy does NOT expand ${VAR} in a header. A recording proxy saw
    the credential arrive as the literal string "${...}", with a control
    confirming the variable WAS in agy's spawn environment at 67 characters --
    so non-expansion, not an unset variable. AI_INSTITUTE_MCP_AGY_LITERAL=1 IS
    required for this hand, and the earlier claim that it is not is withdrawn.

    That makes agy the one named exception to never-write-a-literal. The
    mitigation is the file mode and nothing else, so it is worth saying what the
    residual risk is: an agy literal is a PER-MACHINE value in a NODE-GLOBAL
    file that has been observed copied between hosts, which is how sg01 and
    gubei12 came to present each other's identity. Re-check by hash after any
    copy of this registry.
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

    # COMPARE WHAT THE ENTRY MEANS, NOT HOW IT IS SPELLED, AND MERGE RATHER
    # THAN REPLACE.
    #
    # This used to be `registry.get(n) == wanted[n]` against a freshly built
    # dict, then `registry.update(wanted)`. Measured 2026-09-21: gubei12's
    # entries are shaped {disabled, serverUrl, headers} -- this writer's own
    # shape -- and sg01's are shaped {headers, type, url}, which this writer
    # did not produce and does not model. Both node's hands reach the gateway,
    # so both spellings work.
    #
    # Against a differently-spelled entry the equality can never hold, so the
    # "already current" guard is dead and the first agy task REPLACES the
    # entry: the endpoint moves to `serverUrl` and the unmodelled `type` is
    # dropped, silently, on a configuration this node did not write. It has not
    # fired yet only because sg01 sets no ROS_GATEWAY_MCP_URL, so the server is
    # not in `servers` there at all -- it is armed for the next provisioning
    # run on exactly the node that still needs one.
    #
    # This estate has already broken this file twice in one night. A writer
    # that owns a file it shares with other authors must read what is there
    # before deciding it is wrong.
    changed = []
    for n, e in wanted.items():
        cur = registry.get(n)
        if isinstance(cur, dict) and _entry_url(cur) == e["serverUrl"] \
                and cur.get("headers") == e["headers"] and not cur.get("disabled"):
            continue                      # same endpoint, same auth: leave the spelling alone
        if isinstance(cur, dict):
            # Keep every key this writer does not model, and keep the endpoint
            # under the name already in use rather than renaming it.
            merged = dict(cur)
            key = next((k for k in _URL_KEYS if k in cur), "serverUrl")
            merged[key] = e["serverUrl"]
            merged["headers"] = e["headers"]
            merged["disabled"] = False
            registry[n] = merged
        else:
            registry[n] = e
        changed.append(n)

    if not changed:
        # Already correct. Say so rather than rewriting the file on every task.
        return McpPlan(env=_env_for(servers), note="agy registry already current")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.chmod(path, 0o600)
    return McpPlan(env=_env_for(servers),
                   note="%s <- %s (node-global%s)" % (_AGY_CONFIG, ", ".join(changed),
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


# A SERVER THAT 401s IS WORSE THAN NO SERVER, AND THIS NODE HAD NO WAY TO TELL.
#
# Measured live on sg01, 2026-09-22 23:2x: the operator set AI_INSTITUTE_MCP_URL
# and AI_INSTITUTE_MCP_KEY and did not set AI_INSTITUTE_MCP_HEADER. `settings()`
# resolved happily, defaulted the header to Authorization, defaulted the scheme to
# "Bearer " because the header was Authorization -- and every call 401s. The SAME
# key sent as `X-Admin-Token` returns 57 tools.
#
#   before the URL was set   settings() -> None, no hand got a server   A CLEAN ABSENCE
#   after                    every hand gets a server that 401s          WORSE
#
# And the 401 from the correct key under the wrong header is byte-identical to the
# 401 from a zeroed key -- I ran both. So the hand cannot tell a misconfiguration
# from a revoked credential, and neither could this node.
#
# `settings()` already refuses a URL with no credential for exactly this reason.
# The guard was written for a MISSING credential and the header is a GUESS, which
# the defaulted-header warning admits and then proceeds anyway.
#
# So: ask the endpoint, once per (server, url, credential), before handing the
# config to a model.
PROBE_HEADER = "X-Agent-Route-Probe"
_CRED_VERDICT: dict = {}


def _cred_key(cfg: dict) -> tuple:
    """The triple, not the name. A rotated credential must re-verify.

    epistates 49f62ef2-3ec8-4785-acac-0d3657e3ad23, on a census that compared
    across seats: a capture without its seat is not comparable, and a seat is the
    triple (host, endpoint, key fingerprint) rather than any one of them. The same
    applies to a cached verdict -- keyed on the server NAME it would survive the
    operator fixing the very thing it recorded.
    """
    return (cfg.get("name"), cfg.get("url"), cfg.get("header"), cfg.get("scheme"),
            hashlib.sha256((cfg.get("key") or "").encode()).hexdigest()[:12])


def _probe(url: str, hdrs: dict, timeout: float) -> tuple:
    """(http_status_of_the_GATED_call, body_bytes) or raises.

    INITIALIZE IS NOT THE GATED CALL, AND THIS FUNCTION'S FIRST DRAFT ASKED ONLY THAT.
    Measured on the institute surface from sg01, 2026-09-22:

        initialize, real key           HTTP 200
        initialize, zeroed key         HTTP 200
        initialize, NO CREDENTIAL      HTTP 200   <- the probe could not fail
        tools/list, real key           HTTP 200, 57 tools
        tools/list, NO CREDENTIAL      HTTP 401

    So a verifier built on `initialize` reports "ok" for a node with no credential
    at all. That is this estate's own probe rule -- a control must fail when the
    thing it controls for is broken -- and the first version of this file did not
    meet it. The handshake is not the entitlement.
    """
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "agent-route-node", "version": "1"}}}).encode()
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        resp.read()
    h = dict(hdrs)
    if sid:
        h["Mcp-Session-Id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                       "params": {}}).encode()
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


# Candidate headers, tried ONLY to repair a DEFAULT -- never to override a
# declaration. See _resolve_live_header.
_HEADER_CANDIDATES = (
    ("X-Admin-Token", ""),
    ("X-API-Key", ""),
    ("Authorization", "Bearer "),
)


def verify_server(cfg: dict, timeout: float = 8.0) -> tuple:
    """(verdict, detail) where verdict is "ok" | "refused" | "unknown".

    ONLY AN EXPLICIT 401/403 DISQUALIFIES. A timeout, a DNS failure, a 5xx or a
    malformed body all return "unknown" and the server is still offered.

    This is epistates' rule in 49f62ef2-3ec8-4785-acac-0d3657e3ad23 -- "a warning
    must be right about WHY; a refusal only has to be right about WHETHER" --
    pointed at a refusal this time. A 401 is the endpoint stating that it will not
    serve this credential, which is evidence. A timeout is the absence of evidence,
    and refusing on it would take a working node off MCP because a network blipped.
    """
    key = _cred_key(cfg)
    if key in _CRED_VERDICT:
        return _CRED_VERDICT[key]
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        # Cloudflare answers 403/1010 to an unusual User-Agent, which reads as an
        # auth failure and is not one -- so a bare urllib UA would make this
        # function invent the refusal it exists to detect.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        # THE PROBE SAYS IT IS A PROBE, ON EVERY CALL IT MAKES.
        #
        # It was identifiable only by `agent-route-node` in the handshake's
        # clientInfo, and the handshake is not the only call any more -- so the
        # gated `tools/list` counted as model traffic in this estate's own proxy
        # test. Anything that has to be recognised downstream should say what it
        # is rather than be inferred from an incidental field.
        PROBE_HEADER: "1",
        cfg["header"]: (cfg.get("scheme") or "") + cfg["key"],
    }
    try:
        _probe(cfg["url"], hdrs, timeout)
        verdict = ("ok", "tools/list accepted")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            verdict = ("refused", "HTTP %d" % e.code)
        else:
            verdict = ("unknown", "HTTP %d — not an auth refusal" % e.code)
    except Exception as e:
        verdict = ("unknown", type(e).__name__)
    _CRED_VERDICT[key] = verdict
    return verdict


def _resolve_live_header(cfg: dict, timeout: float = 8.0):
    """Repair a DEFAULTED header by measurement. Never override a DECLARED one.

    THE PREFIX TABLE IS THE DEFECT THIS ESTATE KEEPS SHIPPING, SO IT IS NOT USED.

    rho a3eb1148-1576-465d-818a-586acdad6749 and epistates
    5435d777-af48-4f49-b7df-705ed479eee9 both corrected LFAB-100 within the hour:
    the header that works is a function of the KEY CLASS, and pinning
    `X-Admin-Token` globally would break a node holding a read-class key. rho
    proposed deriving the header from the key PREFIX. Measured at this seat, that
    would fail here:

        the institute's own 401 names  sk_ins_ -> X-API-Key · sk_admin -> X-Admin-Token
        sg01's installed key           nk_c4f7e9...  67 chars   MATCHES NEITHER
        X-Admin-Token                  200, 57 tools
        X-API-Key                      401
        Authorization: Bearer          401

    epistates found a third class too -- 49 chars, neither documented prefix. A
    prefix table is a list of literals in the path that decides what the
    structural test applies to, which is the shape this estate has now shipped
    three times (a tag vocabulary, an extension list, an accumulate-verb list).

    So: ASK. The endpoint knows, and it answers in one call per candidate.

    THE BOUNDARY, and it is the room's own declared-versus-label rule:

        DEFAULTED   this node guessed, so a better guess found by measurement is
                    strictly an improvement -- repair it and say so
        DECLARED    an operator wrote it down. Silently substituting something
                    else would make the .env a decoration and the next debugger's
                    first hypothesis wrong. Report the failure, change nothing.
    """
    hdr_var = (cfg.get("url_var") or "AI_INSTITUTE_MCP_URL").replace("_URL", "_HEADER")
    if (os.getenv(hdr_var) or "").strip():
        return None          # DECLARED -- not ours to second-guess
    for header, scheme in _HEADER_CANDIDATES:
        if header == cfg["header"] and scheme == (cfg.get("scheme") or ""):
            continue         # already tried, already refused
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
            PROBE_HEADER: "1",
            header: scheme + cfg["key"],
        }
        try:
            status, _ = _probe(cfg["url"], hdrs, timeout)
        except urllib.error.HTTPError:
            continue
        except Exception:
            return None      # the network, not the header -- stop guessing
        if status == 200:
            return {"header": header, "scheme": scheme, "var": hdr_var}
    return None


def _header_advice(cfg: dict) -> str:
    """What to change, named -- because a refusal that does not say is a dead end.

    Deliberately NOT a claim about which header is right: this node cannot know
    that. It reports what was sent, whether it was declared or defaulted, and the
    one other header this federation is known to use.
    """
    declared = bool((os.getenv(cfg.get("url_var", "").replace("_URL", "_HEADER")) or "").strip()) \
        if cfg.get("url_var") else False
    var = (cfg.get("url_var", "AI_INSTITUTE_MCP_URL")).replace("_URL", "_HEADER")
    # NOT a prescription. The accepted header is a function of the KEY CLASS and
    # this node cannot know the class from the key -- sg01's is `nk_`, which
    # matches neither prefix the institute's own refusal documents. So report
    # what was sent and where it came from, and let the caller read the
    # endpoint's own 401 body, which states the rule.
    return ("sent %r%s; it was %s. The accepted header depends on the KEY CLASS, "
            "not on the endpoint — read the 401 body, which states the mapping — "
            "and declare the result as %s in the data-dir .env"
            % (cfg["header"], (" with scheme %r" % cfg["scheme"]) if cfg.get("scheme") else "",
               "declared" if declared else "DEFAULTED, not declared", var))


def prepare(hand: str, workspace_dir: str) -> McpPlan:
    """Configure `hand` to reach the institute MCP for this one task.

    Returns an empty plan when the feature is off, when this hand is not in the
    configured set, or when writing the configuration fails. Never raises: a
    task that cannot reach the MCP must still run on the old path, which is
    still delivering its files.
    """
    # EVERY OUTCOME SAYS WHAT IT DECIDED, INCLUDING THE ONES THAT DO NOTHING.
    #
    # Three of the four early returns below used to leave NO line at all, and
    # only a plan with a note ever reached the INFO at the bottom. So a hand that
    # ran with no MCP server configured left a record identical to one that was
    # never asked -- and identical to one where the feature is off.
    #
    # Measured 2026-09-22 on sg01: 3 lines matching "mcp" in five months of
    # task_puller.log, against 273 tasks on 09-22 alone. When the operator
    # reported a hand saying "MCP server institute was not ready for this step"
    # (6b2bbbf1-15d4-4e20-af37-0791d1608437), the node's own record could not say
    # whether that hand had been given a server, because the silence covering
    # "configured" and the silence covering "not configured" are the same
    # silence. That is this estate's own rule -- absence must be inspectable --
    # broken in the one place that would have answered the question in one grep.
    #
    # One line per hand per task. That is the price of being able to answer.
    servers = servers_for(hand)
    if not servers:
        if not settings() and not gateway_settings():
            _logger.info("MCP %s: NOT CONFIGURED — the feature is off on this node "
                         "(no institute and no gateway settings); the hand runs "
                         "with no MCP server", hand)
            return McpPlan()
        note = f"{hand} is in no server's configured hand set"
        _logger.info("MCP %s: NOT CONFIGURED — %s; servers exist on this node but "
                     "none lists this hand", hand, note)
        return McpPlan(note=note)
    kept = []
    for cfg in servers:
        verdict, detail = verify_server(cfg)
        if verdict == "refused":
            repaired = _resolve_live_header(cfg)
            if repaired:
                _logger.warning(
                    "MCP %s: server %r refused %r (%s), and the header was DEFAULTED "
                    "rather than declared — %r works against this endpoint with this "
                    "key, so the server is offered under it. Declare %s=%s in the "
                    "data-dir .env so this is not rediscovered every process.",
                    hand, cfg["name"], cfg["header"], detail, repaired["header"],
                    repaired["var"], repaired["header"])
                cfg = dict(cfg, header=repaired["header"], scheme=repaired["scheme"])
                kept.append(cfg)
                continue
            _logger.error("MCP %s: server %r REFUSED THE NODE'S CREDENTIAL (%s) — "
                          "withholding it rather than handing the hand a server that "
                          "401s inside its tool loop. %s",
                          hand, cfg["name"], detail, _header_advice(cfg))
            continue
        if verdict == "unknown":
            _logger.info("MCP %s: server %r could not be verified (%s) — offering it "
                         "anyway; a refusal must be right about WHETHER, and this is "
                         "not evidence of a bad credential", hand, cfg["name"], detail)
        kept.append(cfg)
    if not kept:
        note = "every configured server refused this node's credential"
        _logger.info("MCP %s: NOT CONFIGURED — %s", hand, note)
        return McpPlan(note=note)
    servers = kept
    writer = _WRITERS.get(hand)
    if not writer:
        note = f"{hand} has no per-task MCP surface yet (stage 2)"
        _logger.info("MCP %s: NOT CONFIGURED — %s; %d server(s) would have been "
                     "offered", hand, note, len(servers))
        return McpPlan(note=note)
    try:
        plan = writer(servers, workspace_dir)
    except Exception as e:
        _logger.warning("MCP setup failed for %s: %s — falling back to the R2 path", hand, e)
        return McpPlan(note=f"setup failed: {e}")
    if plan.note:
        _logger.info("MCP %s: %s", hand, plan.note)
    else:
        # A writer that returns an empty note did something and said nothing.
        _logger.info("MCP %s: writer ran and declared no plan for %d server(s)",
                     hand, len(servers))
    return plan
