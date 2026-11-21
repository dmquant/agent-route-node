"""rsi_shadow_guard: fail-closed enforcement of the rsi-shadow lane on an edge node (ARC-298 Wave 2B).

Pure and stdlib-only so it can be dropped into agent-route-node as app/rsi_shadow_guard.py unchanged.
It makes decisions; it does not run anything. The puller calls guard() with the task, the resolved hand,
the env it would give the hand and the MCP servers it would register, and runs the hand only on
Decision.allowed, using Decision.env / .mcp_servers / .tools (never the originals).

Wire contract, ARC-299 ruling option (i): the pull delivery carries, beside top-level lane "rsi-shadow",
  rsi = {lane: "rsi-shadow", round_id, candidate_id, readOnly: true, credentials: "none",
         tools: "read-only", source: "rsi-shadow", priority: -3}
built by the Worker from rsi_task_meta + tasks. The pull strips context/source/priority, so the guard reads
this block and requires it. A context or top-level source/priority that IS present must agree with it, and
the lane is checked across rsi, context, meta and top level (an explicit null counts as a value).
No lane anywhere -> not lane work, guard() passes it through untouched (allowed, applies False).
A lane that is anything but exactly "rsi-shadow" -> refused. rsi-shadow work is refused unless EVERY
one of: the lane is not contradicted between context / meta / top level; the payload carries the exact
contract values above; the hand declares rsi_shadow_capable is True. Then the hand gets
  - an env built from a small ALLOWLIST (nothing inherited: no keys, no tokens, no HOME-held config)
  - zero MCP servers (a server needs a credential header; an explicit allowlist is the only way back in)
  - a read-only tool set; any requested tool outside it is refused, not dropped silently
Any exception while deciding is a refusal.
"""

LANE = "rsi-shadow"
SOURCE = "rsi-shadow"
PRIORITY = -3
EXIT_REFUSED = 66

READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "LS")
# The only env a shadow hand receives. HOME is deliberately absent: it is where agent CLIs keep tokens.
ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")
_ID_MAX = 200


class Decision(object):
    def __init__(self, allowed, applies, reasons=(), env=None, mcp_servers=None, tools=None):
        self.allowed = allowed
        self.applies = applies
        self.reasons = list(reasons)
        self.env = env
        self.mcp_servers = mcp_servers
        self.tools = tools

    def refusal_text(self):
        return "[RSI_SHADOW_REFUSED] " + "; ".join(self.reasons) + "; the task was not run"


def _lanes(task):
    """(present, lane, problem) over context, meta and top level."""
    found = []
    for key in ("rsi", "context", "meta"):
        box = task.get(key)
        if box is None:
            continue
        if not isinstance(box, dict):
            return True, None, "%s is not an object" % key
        if "lane" in box:
            found.append(box["lane"])
    if "lane" in task:
        found.append(task["lane"])
    if not found:
        return False, None, None
    if any(x != found[0] for x in found[1:]):
        return True, None, "lane differs between context, meta and top level"
    return True, found[0], None


def _id_ok(x):
    return isinstance(x, str) and 0 < len(x) <= _ID_MAX and x == x.strip()


def payload_problems(task):
    """Every way the task departs from the contract payload. Empty list = conforms.
    The authority is the pulled `rsi` block (ARC-299 option i). A legacy `context` and top-level
    source/priority are not required, but whatever of them is present must agree with the block."""
    out = []
    blk = task.get("rsi")
    if not isinstance(blk, dict):
        return ["rsi block is missing or not an object"]
    if blk.get("lane") != LANE:
        out.append("rsi.lane is not %r" % LANE)
    if blk.get("readOnly") is not True:
        out.append("rsi.readOnly is not true")
    if blk.get("credentials") != "none":
        out.append("rsi.credentials is not 'none'")
    if blk.get("tools") != "read-only":
        out.append("rsi.tools is not 'read-only'")
    for k in ("round_id", "candidate_id"):
        if not _id_ok(blk.get(k)):
            out.append("rsi.%s is missing or not a plain non-empty string" % k)
    if blk.get("source") != SOURCE:
        out.append("rsi.source is not %r" % SOURCE)
    if not _priority_ok(blk.get("priority")):
        out.append("rsi.priority is not %d" % PRIORITY)
    if "source" in task and task["source"] != SOURCE:
        out.append("source is not %r" % SOURCE)
    if "priority" in task and not _priority_ok(task["priority"]):
        out.append("priority is not %d" % PRIORITY)
    ctx = task.get("context")
    if isinstance(ctx, dict):
        for ck, bk in (("rsi_round_id", "round_id"), ("candidate_id", "candidate_id"),
                       ("readOnly", "readOnly"), ("credentials", "credentials"), ("tools", "tools")):
            if ck in ctx and ctx[ck] != blk.get(bk):
                out.append("context.%s disagrees with rsi.%s" % (ck, bk))
    return out


def _priority_ok(p):
    return not isinstance(p, bool) and isinstance(p, (int, float)) and p == PRIORITY


def scrub_env(env):
    """A new dict holding only ENV_ALLOW keys that are present. Never mutates or returns the input."""
    return {k: env[k] for k in ENV_ALLOW if isinstance(env, dict) and isinstance(env.get(k), str)}


def scrub_mcp(servers, allow=()):
    """Zero MCP servers unless a server's name is in the explicit allow tuple AND it carries no header,
    env, token or auth field of any kind (a read-only, credential-free server)."""
    keep = []
    for s in servers or ():
        if not isinstance(s, dict) or s.get("name") not in allow:
            continue
        creds = [k for k in s if k.lower() in ("headers", "env", "token", "auth", "authorization", "api_key", "key")
                 or "token" in k.lower() or "secret" in k.lower() or "key" in k.lower()]
        if not creds:
            keep.append(dict(s))
    return keep


def tools_problems(requested):
    """Requested tools outside READ_ONLY_TOOLS, as problem strings. None/empty request = the read-only set."""
    if requested is None:
        return []
    if not isinstance(requested, (list, tuple)):
        return ["tools is not a list"]
    return ["tool %r is not read-only" % (t,) for t in requested if t not in READ_ONLY_TOOLS]


def guard(task, hand, env, servers=(), requested_tools=None, mcp_allow=()):
    try:
        if not isinstance(task, dict):
            return Decision(False, True, ["task is not an object"])
        present, lane, problem = _lanes(task)
        if problem:
            return Decision(False, True, [problem])
        if not present:
            return Decision(True, False, env=env, mcp_servers=servers, tools=requested_tools)
        if lane != LANE:
            return Decision(False, True, ["unknown lane %r" % (lane,)])
        reasons = payload_problems(task)
        if getattr(hand, "rsi_shadow_capable", False) is not True:
            reasons.append("hand %r is not rsi_shadow_capable" % (getattr(hand, "name", "?"),))
        reasons += tools_problems(requested_tools)
        if reasons:
            return Decision(False, True, reasons)
        return Decision(True, True, env=scrub_env(env), mcp_servers=scrub_mcp(servers, mcp_allow),
                        tools=list(READ_ONLY_TOOLS if requested_tools is None else requested_tools))
    except Exception as e:  # fail closed
        return Decision(False, True, ["guard error %s" % type(e).__name__])
