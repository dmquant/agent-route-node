#!/usr/bin/env python3
"""Does this hand's MCP configuration actually reach the network from a dispatch?

    python3 scripts/verify_hand_mcp.py --live opencode
    python3 scripts/verify_hand_mcp.py --live --all

TWO MODES, AND ONLY ONE OF THEM GIVES A VERDICT
-----------------------------------------------
Default (trap): a canned listener. It answers whatever arrives, so it can only
say "a packet left the hand". It CANNOT say the config is good, because it is
not the institute and it cannot finish an MCP handshake -- measured on Studio02,
a JSON trap made opencode report `Invalid content type` and an SSE trap made it
report `SSE error: undefined`, both times after opencode had dialled correctly.
So this mode reports REACHED, never VERIFIED, and its failures are not evidence.

--live: a recording proxy in front of the real institute, FORWARDING the hand's
own credential. The real server does the handshake; this watches. It can tell
the three cases apart that matter -- never dialled, dialled and was refused,
dialled and was served -- and only the last is VERIFIED.

Exit codes, three because two would lie:

    0  verified        the hand binary itself completed a round trip to the trap
    1  failed          no round trip -- broken invocation, or a config that does
                       not apply under dispatch
    2  not applicable  this hand is not configurable on this node (mcp_config
                       declined it), so there is nothing to verify

WHY THIS EXISTS, AND WHY IT IS NOT A CONFIG READER
--------------------------------------------------
Reading a config file proves the file says the right thing. It cannot prove the
hand READS that file under a dispatch. The case that forced this: `claude`'s
other config surface (~/.claude.json mcpServers) is keyed by working directory,
and a dispatched hand runs in a FRESH per-task workspace matching no configured
project -- so an entry written the obvious way tests green by hand and does
nothing under dispatch. No amount of careful reading detects that. Running the
binary and watching for a packet does.

Adopted from epistates' AAL-057, which was itself paid for: a seat was never
playing because python3.14 on that host could not open a LAN socket while
python3.11 and curl on the SAME host could.

THE REFUSAL THAT MAKES IT HONEST
--------------------------------
The trap MUST bind a LAN address, never loopback. macOS Local Network privacy
is not exercised by a request to 127.0.0.1, so a loopback verify passes on a
host where the real traffic cannot leave -- a guard that cannot fail. If no
non-loopback address can be found, this REFUSES (exit 1) rather than falling
back to 127.0.0.1 and reporting success.
"""
import argparse
import datetime
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import mcp_config  # noqa: E402
from app.config import get_env_path  # noqa: E402
from app.hands.base import resolve_cli_path, get_cli_env  # noqa: E402

# Load the node's OWN .env before anything reads a setting.
#
# Without this the script inherits whatever shell launched it, and an earlier
# run of this harness graded AI_INSTITUTE_MCP_HEADER as unset -- reporting a
# node defect that was really an unloaded file. A guard must read the node's
# configuration the way the daemon reads it, or it is measuring the operator's
# terminal.
#
# Parsed here rather than via python-dotenv ON PURPOSE. The daemon runs under a
# venv that has it; this script is run ad hoc with whatever python3 is on PATH,
# and on Studio02 that one does not. Wrapped in try/except, the missing import
# turned into "URL is unset" -- a guard blaming the node for its own missing
# dependency, which is the exact failure shape it exists to refuse. A dozen
# lines with no dependency cannot fail that way.
def load_node_env(path: str) -> int:
    if not path or not os.path.exists(path):
        return -1
    n = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)       # a real shell value still wins
            n += 1
    return n

ENV_PATH = get_env_path()
ENV_VARS = load_node_env(ENV_PATH)

VERIFIED, FAILED, NOT_APPLICABLE = 0, 1, 2

# What each hand is asked to do. It must name ONE tool and ONE argument.
#
# Two prompts failed here first, and both failed the same way -- they asked for
# something reachable without the server:
#
#   "answer X"                     -> answered from the model's own head
#   "list the tools available"     -> opencode went reading ~/.config/opencode/
#                                     *.json to enumerate them, hit a permission
#                                     wall, and exited in 5s having never dialled
#
# Both would be scored FAILED, and both verdicts would be about the prompt. A
# named tool with a named argument has no answer anywhere but the endpoint.
#
# sessions_get_files is chosen because it is the file-delivery surface this
# whole workstream is about, and because it returns a real result for an
# unknown session (files: [], source: archive) rather than an error -- so a
# non-200 in the log is auth or transport, never "no such session".
# The prompt is now per-server -- see SERVERS and prompt_for(). It must still
# name ONE tool and ONE argument, for the reason recorded above.

# HOW EACH HAND IS INVOKED -- AND THE STANDING RISK IN THIS TABLE.
#
# This gate's entire claim is that it runs what a dispatch runs. A hand-rolled
# argv breaks that claim silently, and it has twice:
#
#   agy, no -p        the prompt was passed bare; agy reads it only from
#                     -p/--print/-i/stdin, so it exited with `unexpected
#                     argument` having never started. Scored "its config does
#                     not apply under dispatch".
#   agy, no flags     production appends --add-dir and
#                     --dangerously-skip-permissions (app/hands/agy_hand.py).
#                     Without them agy's headless mode AUTO-DENIES the `mcp`
#                     permission -- so it connects, lists the tools, and is
#                     blocked from calling one. That looks exactly like a
#                     dispatch-path config defect and is a missing flag.
#
# Both were read as node findings before being traced back here. Every entry
# below must mirror app/hands/<hand>_hand.py; when a hand's invocation changes
# there and not here, this gate starts grading a command nobody runs.
HAND_CMD = {
    "opencode": lambda cmd, args, p, ws: [cmd, "run", *args, p],
    "claude":   lambda cmd, args, p, ws: [cmd, "@anthropic-ai/claude-code", "-p",
                                          "--dangerously-skip-permissions", *args, p],
    "codex":    lambda cmd, args, p, ws: [cmd, "codex", "exec", "--skip-git-repo-check",
                                          "--dangerously-bypass-approvals-and-sandbox",
                                          *args, p],
    # --print is value-taking and consumes the NEXT token, so it must be last
    # and the prompt must be its immediate value.
    "agy":      lambda cmd, args, p, ws: [cmd, *args, "--add-dir", ws,
                                          "--dangerously-skip-permissions",
                                          "--print", p],
}
HAND_BIN = {"opencode": "opencode", "claude": "npx", "codex": "npx", "agy": "agy"}


# Hands whose own credential lives in the macOS keychain, which a process
# descended from an ssh session cannot read.
KEYCHAIN_HANDS = {"claude"}


def ssh_descended() -> bool:
    return any(os.getenv(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"))


# A hand that cannot authenticate ITSELF never reached the MCP question. Its
# tool list may still have loaded -- the MCP client connects before the model
# runs -- so the probe sees a clean handshake and no tools/call, which is
# exactly the shape of a config defect and is not one.
HAND_AUTH_FAILURE = ("oauth session expired", "could not be refreshed",
                     "please run /login", "invalid api key", "not authenticated",
                     "authentication_error")


def lan_address() -> str:
    """A non-loopback IPv4 address of this host, or "" if there is none.

    Uses a UDP connect to a routable address: no packet is sent, but the kernel
    picks the source address it WOULD use, which is the one a peer could reach.
    Reading interfaces directly would also return addresses that do not route.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))          # TEST-NET-1, never routed
        addr = s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()
    return "" if addr.startswith("127.") else addr


class Trap(http.server.BaseHTTPRequestHandler):
    """Answers anything, records that it was reached at all."""
    hits: list = []

    def _reply(self):
        Trap.hits.append((self.path, self.headers.get("User-Agent", "")))
        # MUST be text/event-stream, not application/json.
        #
        # Measured on Studio02: with a JSON trap, opencode dialled it TWICE and
        # then reported `SSE error: Invalid content type, expected
        # "text/event-stream"`. The hand was reaching the trap and the trap was
        # rejecting the hand -- so the guard scored a config that WORKS as a
        # failure, and the verdict measured this file rather than the node.
        # A probe that speaks the wrong protocol tests itself.
        payload = ('{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05",'
                   '"capabilities":{"tools":{}},'
                   '"serverInfo":{"name":"verify-trap","version":"1"}}}')
        body = ("event: message\ndata: %s\n\n" % payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        try:
            self.wfile.flush()
        except Exception:
            pass

    do_POST = _reply
    do_GET = _reply

    def log_message(self, *a):
        pass                                  # the caller reports, not the server


def rpc_error_in(body: bytes):
    """The JSON-RPC error message in this response, or None.

    Handles both plain JSON and SSE framing (`data: {...}`), because a
    streamable-HTTP server may answer either and the gate must not care.
    Returns None when there is no error OR when the body cannot be parsed --
    an unparseable body is reported as unknown by the caller, never as success.
    """
    if not body:
        return None
    text = body.decode("utf-8", "replace")
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        err = d.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:160]
        # A tool can also fail INSIDE a successful result.
        res = d.get("result")
        if isinstance(res, dict) and res.get("isError"):
            return "result.isError: %s" % str(res.get("content"))[:120]
    return None


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_proxy(upstream: str):
    """A recording proxy in front of the REAL institute, not a fake server.

    Why this replaces the trap for any verdict that has to be trusted:

    A canned trap cannot finish an MCP handshake. Measured on Studio02, a JSON
    trap drew `Invalid content type, expected "text/event-stream"` and a single
    SSE frame drew `SSE error: undefined`. Both times opencode DIALLED and both
    times the PROBE rejected the HAND. A guard whose failures mean "my fake is
    not a server" fails configs that work.

    So let the real server be the server. What this adds is observation.

    It FORWARDS the hand's own headers rather than injecting a credential. That
    is the point: the node delivers the credential into harness config, the
    harness expands it into the request, and the institute grants or denies. A
    proxy supplying its own auth would test transport and silently skip the half
    OP-002/OP-003 are about -- and would report `codex` VERIFIED, when Bearer is
    exactly what the institute rejects.

    BE A PROXY, NOT AN APPROXIMATION OF ONE. Two shortcuts here each produced a
    clean-looking upstream failure that was entirely this file's doing:

      do_GET = do_POST     streamable-HTTP opens its notification stream with
                           GET. Aliased to POST it arrived upstream as a POST
                           with an empty body and came back 400 -- read as "the
                           institute rejects opencode".
      forwarding only      `initialize` answers with Mcp-Session-Id, and every
      Content-Type         later request carries it back. Dropping it breaks the
                           session on request two, and the hand reports a
                           protocol error it did not cause.

    So: each method relays as itself, every non-hop-by-hop response header goes
    back, and the body streams rather than being read whole -- an SSE stream
    read with .read() does not return until the server closes it.
    """
    import urllib.request
    import urllib.error

    # Hop-by-hop headers belong to a single connection and must not be relayed.
    HOP = {"connection", "keep-alive", "te", "trailer", "transfer-encoding",
           "upgrade", "proxy-authorization", "proxy-authenticate"}
    DROP_REQ = HOP | {"host", "content-length"}
    DROP_RES = HOP | {"content-length"}      # length changes when we stream

    class Proxy(http.server.BaseHTTPRequestHandler):
        calls: list = []

        def _relay(self, method: str):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else None
            try:
                name = json.loads(raw or b"{}").get("method") or "-"
            except Exception:
                name = "-"

            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in DROP_REQ}
            # Cloudflare answers 403 error 1010 to an unusual User-Agent, which
            # reads as "auth fails everywhere" on every arm of the test.
            fwd["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            authed = any(k.lower() in ("authorization", "x-admin-token")
                         for k in fwd)
            # Did the HAND expand its ${VAR} reference, or send it literally?
            #
            # A credential that arrives as the STRING "${ROS_GATEWAY_MCP_KEY}"
            # is a 401 that looks exactly like a wrong key, and the difference
            # decides whether an env reference is usable on this hand at all.
            # Reported as a boolean about the shape -- never the value.
            unexpanded = any(("${" in v or "{env:" in v)
                             for k, v in fwd.items()
                             if k.lower() in ("authorization", "x-admin-token"))

            req = urllib.request.Request(upstream, data=raw, method=method,
                                         headers=fwd)
            try:
                up = urllib.request.urlopen(req, timeout=60)
                code, hdrs = up.status, up.headers
            except urllib.error.HTTPError as e:
                up, code, hdrs = e, e.code, e.headers
            except Exception as exc:
                Proxy.calls.append({"method": "%s %s" % (method, name), "code": 0,
                                    "authed": authed, "error": str(exc)})
                self.send_error(502, "proxy could not reach the institute")
                return

            Proxy.calls.append({"method": name if method == "POST"
                                else "%s(stream)" % method,
                                "code": code, "authed": authed,
                                "unexpanded": unexpanded, "at": utc_now()})
            self.send_response(code)
            for k, v in hdrs.items():
                if k.lower() not in DROP_RES:
                    self.send_header(k, v)
            self.end_headers()

            # Keep a bounded copy of the body WHILE streaming it, because
            # HTTP 200 IS NOT SUCCESS FOR JSON-RPC.
            #
            # A JSON-RPC error -- unknown tool, bad arguments, a refusal --
            # rides inside a 200 as an `error` member. This gate certified
            # codex on the status code alone, which is the same class of
            # mistake as reading a tools/list 200 as proof of auth: a signal
            # adjacent to the thing, accepted instead of the thing.
            seen, cap = [], 65536
            total = 0
            try:
                while True:
                    chunk = up.read(8192)
                    if not chunk:
                        break
                    if total < cap:
                        seen.append(chunk[:cap - total])
                        total += len(chunk)
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass                          # the hand hung up; that is its right
            finally:
                try:
                    up.close()
                except Exception:
                    pass
            Proxy.calls[-1]["rpc_error"] = rpc_error_in(b"".join(seen))

        def do_POST(self):
            self._relay("POST")

        def do_GET(self):
            self._relay("GET")

        def do_DELETE(self):
            self._relay("DELETE")

        def log_message(self, *a):
            pass

    return Proxy


def dispatched_hands():
    """Which hands the DAEMON actually runs. (list, "") or (None, reason).

    FOURTH TIME A DOCUMENTED LESSON HAS BEEN REPEATED HERE, so it is code now.

    scripts/supervise.py carries the same scar: it hard-coded opencode and told
    sg01 it was broken while all four of its hands were healthy. The repair was
    to ask the DAEMON what it runs, "because the daemon is the thing whose view
    decides whether work runs."

    This gate was studio-shaped in exactly the same way. Measured on gubei12:
    ENABLE_OPENCODE=false -- opencode is not dispatched there at all -- and
    --all spent 840 seconds grading it across two runs and reported FAILED
    twice. A verdict about a hand the node never invokes is noise that looks
    like a defect.

    Unreadable is UNKNOWN, not empty. supervise.py's second repair read the
    in-process registry, which is empty outside the daemon, and the script
    then reported healthy having tested nothing. So None here means "could not
    ask", and the caller must say so rather than silently verify nothing.
    """
    import urllib.request
    try:
        req = urllib.request.Request("http://localhost:8017/api/hands/health",
                                     headers={"User-Agent": "agent-route-node/verify"})
        h = json.load(urllib.request.urlopen(req, timeout=20)).get("health", {})
    except Exception as e:
        return None, repr(e)[:70]
    if not h:
        return None, "daemon returned no hand registry"
    return [n for n, v in h.items() if v.get("enabled")], ""


def other_servers(hand: str) -> list:
    """Every OTHER MCP server this hand can reach during the run.

    THE GATE'S BLIND SPOT, AND WHY A VERDICT IS INCOMPLETE WITHOUT THIS.

    Measured on gubei12: `agy` was scored FAILED -- connected, listed, no
    tools/call -- while its stdout carried the CORRECT institute payload,
    byte-identical to a direct call. It had not fabricated it. Its node-global
    registry holds `ros-gateway`, a second server fronting the same estate with
    a LITERAL nk_ token in two headers, and the hand used that instead.

    So the gate configured server X, watched X, and graded a hand that was
    quietly succeeding through server Y. A FAILED verdict in that state is
    false, and a VERIFIED one would have been luck.

    This is also the contract's own governed_coverage warning, measured rather
    than predicted: actual access is higher than the governed number and is
    unattributed. Reported, never touched -- these are the operator's servers,
    and their credentials are not this script's to move.
    """
    path = NodeGlobalConfig.PATHS.get(hand)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            reg = json.load(fh).get("mcpServers") or {}
    except Exception:
        return []
    out = []
    for name, spec in reg.items():
        if name == (os.getenv("AI_INSTITUTE_MCP_NAME") or "ai-institute").strip():
            continue
        target = spec.get("serverUrl") or spec.get("url") or spec.get("command") or "?"
        # Name the RISK, never the value. A literal-looking credential is
        # reported as a fact about the file, not quoted out of it.
        literal = any(isinstance(v, str) and v and "${" not in v and "{env:" not in v
                      for v in (spec.get("headers") or {}).values())
        out.append({"name": name, "target": target, "literal_credential": literal})
    return out


class NodeGlobalConfig:
    """Snapshot and restore a config this verify would otherwise clobber.

    `agy` has no per-task scope, so mcp_config writes its entry into the
    NODE-GLOBAL registry. Verifying it therefore points the operator's real agy
    at a trap that dies with this process, and would leave it there. Deleting
    the entry afterwards is not a fix either: if a real entry was configured,
    deleting it disables the live config.

    So: byte-for-byte snapshot in, byte-for-byte restore out, including absence
    (a file this created is removed). A guard that damages what it inspects is
    worse than no guard.
    """

    PATHS = {"agy": os.path.expanduser("~/.gemini/config/mcp_config.json")}

    def __init__(self, hand: str):
        self.path = self.PATHS.get(hand)
        self.before = None
        self.existed = False

    def __enter__(self):
        if self.path and os.path.exists(self.path):
            self.existed = True
            with open(self.path, "rb") as fh:
                self.before = fh.read()
        return self

    def __exit__(self, *exc):
        if not self.path:
            return False
        if self.existed:
            with open(self.path, "wb") as fh:
                fh.write(self.before)
            os.chmod(self.path, 0o600)
        elif os.path.exists(self.path):
            os.remove(self.path)
        return False

    def unchanged(self) -> bool:
        """For the caller to assert restoration actually happened."""
        if not self.path or not self.existed:
            return True
        with open(self.path, "rb") as fh:
            return fh.read() == self.before


# WHICH GOVERNED SERVER IS UNDER TEST.
#
# Two, because they authenticate differently and that difference decides which
# hands can reach them. codex can express ONLY Authorization: Bearer; the
# institute rejects Bearer and the gateway requires it. A gate that only ever
# tested the institute would keep reporting codex as N/A forever while 73 tools
# sat reachable.
#
# Each entry names its own tool, because the probe prompt has to name ONE tool
# and ONE argument -- a prompt the model can satisfy without the server is how
# this gate produced three verdicts about itself.
SERVERS = {
    "institute": {
        "url_var": "AI_INSTITUTE_MCP_URL",
        "key_vars": ("AI_INSTITUTE_MCP_KEY", "NODE_TOKEN"),
        "tool": "sessions_get_files",
        "args": '{"session_id": "verify-probe"}',
        "name_var": "AI_INSTITUTE_MCP_NAME",
        "default_name": "ai-institute",
    },
    "gateway": {
        "url_var": "ROS_GATEWAY_MCP_URL",
        "key_vars": ("ROS_GATEWAY_MCP_KEY", "NODE_TOKEN"),
        # Namespaced by the gateway. rho__ is chosen over institute__ because
        # rho's channel is quiet, so a probe call is separable in its records.
        "tool": "rho__rho_projection_meta",
        "args": "{}",
        "name_var": "ROS_GATEWAY_MCP_NAME",
        "default_name": "ros-gateway",
    },
}


def prompt_for(server: str) -> str:
    """Name the SERVER as well as the tool.

    agy does not call a tool by name -- it calls call_mcp_tool(ServerName,
    ToolName, Arguments). Given only `rho__rho_projection_meta` it SPLIT the
    namespaced name and guessed ServerName "rho", which does not exist, and
    returned `invalid tool call`. The gate scored that as the hand declining
    to call, when the hand had tried and been handed an under-specified target.

    So the prompt names both. It costs the other hands nothing -- they address
    tools by the flat namespaced name and ignore the extra fact.
    """
    spec = SERVERS[server]
    name = (os.getenv(spec["name_var"]) or spec["default_name"]).strip()
    return ('Call the tool %s on the MCP server named %s, with arguments %s, '
            'and print its raw result. The server name is exactly "%s" — do not '
            'infer it from the tool name. Call the tool. Do not read any local '
            'file and do not answer from memory.'
            % (spec["tool"], name, spec["args"], name))


TOOLS_LIST = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'


def _control(probe_url: str, workspace: str, auth: str) -> str:
    """Prove the probe is reachable from this host, using a SEPARATE binary.

    Not a nicety. AAL-057 was paid for by a host where python3.14 could not open
    a LAN socket while python3.11 and curl on that same host could. If the probe
    is a Python process, a Python control shares its failure modes and proves
    nothing; curl is a different stack.

    The credential goes in a 0600 config file, never argv -- `ps` on a shared
    node shows argv to every user, and the contract's own acceptance test for
    ad-hoc tool calls is "ps shows no secret in argv". A guard is not exempt
    from the rule it enforces.
    """
    cfg = os.path.join(workspace, ".curlrc-probe")
    lines = ['url = "%s"' % probe_url, 'request = "POST"',
             'header = "Content-Type: application/json"',
             'header = "Accept: application/json, text/event-stream"',
             'data = %s' % ('"' + TOOLS_LIST.replace('"', '\\"') + '"'),
             'silent', 'output = "/dev/null"', 'write-out = "%{http_code}"',
             'max-time = "25"']
    if auth:
        lines.insert(2, 'header = "%s"' % auth)
    with open(cfg, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(cfg, 0o600)
    try:
        r = subprocess.run(["curl", "-K", cfg], capture_output=True, text=True)
        return r.stdout.strip()
    finally:
        os.remove(cfg)


def verify(hand: str, timeout: int, live: bool, server: str = "institute") -> int:
    # THIRD TIME THIS SHAPE HAS COST SOMETHING HERE, so it is a refusal now.
    #
    # STAGE1.md already records it: claude keeps its OAuth credential in the
    # macOS keychain, and a process descended from an ssh session cannot read
    # it. An earlier run of THIS script over ssh produced
    # `OAuth session expired and could not be refreshed` -- and because the MCP
    # client connects before the model runs, the probe saw a clean handshake
    # and no tools/call, which is indistinguishable from a config defect.
    #
    # Reporting FAILED there would have been the same error STAGE1 was
    # corrected for: a test run over ssh is not a test of production.
    if hand in KEYCHAIN_HANDS and ssh_descended():
        print("  %-9s REFUSED  this session is ssh-descended and %s reads its own "
              "credential from the macOS keychain, which such a process cannot open. "
              "The MCP client still connects, so a verdict here would look like a "
              "config defect and be an artefact of the shell. Run it from the launchd "
              "GUI-domain agent on that node." % (hand, hand))
        return FAILED

    addr = lan_address()
    if not addr:
        print("  %-9s REFUSED  no non-loopback address on this host; a loopback "
              "verify cannot fail and would report a false pass" % hand)
        return FAILED

    spec = SERVERS[server]
    upstream = (os.getenv(spec["url_var"]) or "").strip()
    key = next((os.getenv(v) for v in spec["key_vars"] if (os.getenv(v) or "").strip()), "")
    key = (key or "").strip()
    if live and not (upstream and key):
        where = ("%s (%d vars read)" % (ENV_PATH, ENV_VARS) if ENV_VARS >= 0
                 else "%s -- WHICH DOES NOT EXIST" % ENV_PATH)
        print("  %-9s REFUSED  --live needs the real endpoint and credential for %r; "
              "%s is unset after reading %s"
              % (hand, server, spec["url_var"] if not upstream else
                 " or ".join(spec["key_vars"]), where))
        return FAILED

    # ONE PROXY PER CONFIGURED SERVER, not one for the server under test.
    #
    # Redirecting only the tested server left every OTHER governed server
    # pointing at production. agy loads all of its configured servers at
    # startup, so each run opened live connections this gate did not proxy,
    # count, or declare -- and the "declared window" was therefore accurate and
    # incomplete at once, which reads as a full accounting and is not one.
    #
    # Now every server the hand is given goes through a proxy to its own real
    # upstream. The traffic still reaches production, because that is what
    # makes a live verdict worth anything -- but all of it is observed,
    # attributable and inside the declared window. The verdict is still judged
    # on the server under test alone; the rest are reported as observed.
    srvs, probes, recorders, saved_urls = [], {}, {}, {}

    def stop_all():
        """Shut every proxy AND put every redirected URL back.

        --all calls verify() once per hand in one process. A URL left pointing
        at a dead probe would silently mis-configure every later hand, and the
        gate would report findings about a server nobody is running.
        """
        for sv in srvs:
            try:
                sv.shutdown()
            except Exception:
                pass
        for var, old in saved_urls.items():
            if old is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = old

    if live:
        for cfg in mcp_config.servers_for(hand):
            h = make_proxy(cfg["url"])
            sv = http.server.ThreadingHTTPServer((addr, 0), h)
            threading.Thread(target=sv.serve_forever, daemon=True).start()
            srvs.append(sv)
            probes[cfg["name"]] = "http://%s:%d/api/mcp" % (addr, sv.server_address[1])
            recorders[cfg["name"]] = h.calls
            saved_urls[cfg["url_var"]] = os.environ.get(cfg["url_var"])
            os.environ[cfg["url_var"]] = probes[cfg["name"]]
        tested = (os.getenv(spec["name_var"]) or spec["default_name"]).strip()
        recorder = recorders.get(tested)
        if recorder is None:
            print("  %-9s N/A      %r is not configured for this hand" % (hand, tested))
            stop_all()
            return NOT_APPLICABLE
        probe_url = probes[tested]
        srv = srvs[0]
    else:
        handler = Trap
        recorder = Trap.hits
        srv = http.server.ThreadingHTTPServer((addr, 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        srvs = [srv]
        probe_url = "http://%s:%d/api/mcp" % (addr, srv.server_address[1])

    guard = NodeGlobalConfig(hand)
    guard.__enter__()
    # This gate HOLDS the snapshot/restore guard, so it is the one caller
    # entitled to point a node-global registry at a LAN probe.
    os.environ["AI_INSTITUTE_MCP_ALLOW_LAN"] = "1"
    workspace = tempfile.mkdtemp(prefix="verify-%s-" % hand)
    # ONLY the settings under test. Deliberately not dict(os.environ).
    #
    # get_cli_env(workspace) sets PWD to the workspace because opencode
    # resolves its project from PWD, not getcwd() -- measured, and documented
    # at app/hands/base.py. Overlaying os.environ on top of it puts the
    # OPERATOR's PWD back, so the hand looks for opencode.json in the directory
    # this script was launched from, finds none, and runs with no MCP server at
    # all. That is precisely the dispatch-path defect this gate exists to
    # detect, and the gate was manufacturing it.
    overrides = {spec["url_var"]: probe_url}
    if not live:
        overrides[spec["key_vars"][0]] = "nk_verify_probe"
    elif key:
        overrides[spec["key_vars"][0]] = key

    # Build the plan through the SAME code path a dispatch uses. If mcp_config
    # declines this hand, that is a real answer, not an error.
    # In live mode every server's URL was already redirected above. In trap
    # mode only the one under test is, because there is nothing to forward to.
    before = os.environ.get(spec["url_var"])
    if not live:
        os.environ[spec["url_var"]] = probe_url
        os.environ.setdefault(spec["key_vars"][0], "nk_verify_probe")
    try:
        plan = mcp_config.prepare(hand, workspace)
    finally:
        if not live:
            if before is None:
                os.environ.pop(spec["url_var"], None)
            else:
                os.environ[spec["url_var"]] = before

    if not plan and not plan.note.startswith("~/"):
        print("  %-9s N/A      %s" % (hand, plan.note or "not configurable on this node"))
        stop_all(); guard.__exit__()
        return NOT_APPLICABLE

    # POSITIVE CONTROL, before the hand is allowed to be judged.
    #
    # "No packet arrived" has two causes the probe cannot tell apart: the hand
    # never dialled, or nothing on this host could have reached the probe at
    # all. Without a control that FIRES, a FAILED verdict is uninterpretable --
    # and reporting it anyway is the well-formed zero this guard exists to
    # refuse. In --live it proves one thing more: the institute is up and this
    # node's credential works, so a later 401 from the hand is the HAND's.
    auth = ""
    if live:
        if server == "gateway":
            auth = "Authorization: Bearer %s" % key
        else:
            scheme = os.getenv("AI_INSTITUTE_MCP_SCHEME") or ""
            auth = "%s: %s%s" % (mcp_config._resolve_header(), scheme, key)
    code = _control(probe_url, workspace, auth)
    if code != "200" or not recorder:
        print("  %-9s REFUSED  positive control did not fire: curl to %s returned %r "
              "and the probe saw %d request(s). A FAILED verdict here would be "
              "unreadable, so none is given."
              % (hand, probe_url, code, len(recorder)))
        stop_all(); guard.__exit__()
        return FAILED
    control_n = len(recorder)

    others = other_servers(hand)

    # UNDECLARED TRAFFIC THIS RUN WILL GENERATE, named before it happens.
    #
    # Only the server under test is redirected at the probe. Every OTHER
    # governed server keeps its real URL, so a hand that loads all of its
    # configured servers at startup -- agy does, from the node-global registry
    # -- opens initialize/tools/list against LIVE endpoints this gate is not
    # testing and therefore does not observe, count, or declare.
    #
    # Found on 2026-09-20 when another estate asked where ~18 gateway calls on
    # gubei12 came from and part of the answer was this. The declared window
    # below covers only the proxied server; these calls are outside it.
    cmd = resolve_cli_path(HAND_BIN[hand])
    argv = HAND_CMD[hand](cmd, plan.args, prompt_for(server), workspace)
    spawn_env = get_cli_env(workspace)
    spawn_env.update(overrides)
    spawn_env.update(plan.env)
    if spawn_env.get("PWD") != workspace:
        print("  %-9s REFUSED  PWD is %r, not the workspace. opencode resolves its "
              "project from PWD; a run from the wrong one tests nothing."
              % (hand, spawn_env.get("PWD")))
        stop_all(); guard.__exit__()
        return FAILED

    t0 = time.time()
    tail, timed_out = "", False
    try:
        r = subprocess.run(argv, cwd=workspace, env=spawn_env, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tail = ((r.stderr or b"") + (r.stdout or b"")).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        # A hit during the run still counts -- but keep the output, because a
        # hand that hangs without dialling says why in it.
        tail = ((e.stderr or b"") + (e.stdout or b"")).decode("utf-8", "replace")
    except FileNotFoundError:
        print("  %-9s N/A      binary not installed on this node (%s)" % (hand, HAND_BIN[hand]))
        stop_all(); guard.__exit__()
        return NOT_APPLICABLE
    finally:
        stop_all()
        guard.__exit__()
        if not guard.unchanged():
            print("  %-9s REFUSED  node-global config was NOT restored; refusing to "
                  "report a result from a run that changed what it inspected" % hand)
            return FAILED

    dt = time.time() - t0

    # DECLARE THE WINDOW. A certification run is production traffic against a
    # shared, counted channel -- the gateway attributes every forwarded call to
    # this node, and another estate may be measuring that counter right now.
    #
    # Measured cost of not doing this, 2026-09-20: two codex certification runs
    # authorised by OP-017 landed real tools/call requests on rho's namespace
    # minutes after a marker was placed there, moved edge-node:gubei12's
    # last_seen past the marker window, and made a pre-registered verification
    # unreadable. Both asks were on this estate's desk in the same tick.
    #
    # So the gate now says what it did and when, in the same form a marker
    # declares, and the reader need not ask.
    stamped = [c for c in recorder[control_n:] if isinstance(c, dict) and c.get("at")]
    if live and stamped:
        print("  %-9s          declared window: %s .. %s UTC — %d forwarded call(s) "
              "attributed to THIS node at the endpoint"
              % ("", stamped[0]["at"], stamped[-1]["at"], len(stamped)))

    # The OTHER servers the hand loaded. Observed, not judged -- but counted,
    # because an accounting that silently omits them reads as complete and is
    # not. This is the half that was missing when another estate could not
    # reconcile a gateway counter against a number this gate had published.
    if live:
        for name, calls in recorders.items():
            if name == tested or not calls:
                continue
            times = [c["at"] for c in calls if c.get("at")]
            print("  %-9s          also observed: %d call(s) to %s%s — the hand loaded it "
                  "at startup; proxied and counted, not judged"
                  % ("", len(calls), name,
                     (" (%s .. %s UTC)" % (times[0], times[-1])) if times else ""))

    for o in others:
        print("  %-9s          ⚠ also reachable during this run: %s -> %s%s"
              % ("", o["name"], str(o["target"])[:60],
                 "  [literal credential on disk]" if o["literal_credential"] else ""))
    if others:
        print("  %-9s          a verdict below describes the server THIS gate "
              "configured. The hand could satisfy the task through one of the above "
              "without touching it." % "")
    mine = recorder[control_n:]               # the control's own hit is not the hand's
    if not mine:
        # Show what the hand said. "It did not dial" is a symptom; the hand
        # almost always names the cause, and a verdict nobody can act on is
        # only marginally better than no verdict.
        print("  %-9s FAILED   control fired but the hand sent nothing to %s in %.1fs. "
              "The probe is reachable from this host, so the hand ran and did not dial: "
              "its config does not apply under dispatch." % (hand, probe_url, dt))
        for line in [l for l in tail.strip().splitlines() if l.strip()][-6:]:
            print("           | %s" % line[:160])
        return FAILED

    if not live:
        path, ua = mine[0]
        print("  %-9s REACHED  round trip in %.1fs -- %d request(s), first %s (%s). "
              "Transport only: the trap is not the institute, so this says nothing "
              "about auth. Use --live for a verdict." % (hand, dt, len(mine), path,
                                                         (ua or "no UA")[:24]))
        return VERIFIED

    ok = [c for c in mine if c["code"] == 200]
    codes = sorted({c["code"] for c in mine})
    methods = sorted({c["method"] for c in mine})
    if not ok:
        print("  %-9s FAILED   dialled the institute %d time(s) in %.1fs and every call "
              "was refused: HTTP %s on %s. The config APPLIES -- the credential it "
              "carries does not authenticate.%s"
              % (hand, len(mine), dt, ",".join(str(c) for c in codes), ",".join(methods),
                 "" if any(c["authed"] for c in mine) else " No auth header reached the probe."))
        return FAILED

    # VERIFIED REQUIRES A SERVED tools/call, not a connection.
    #
    # tools/list needs no auth on this endpoint. That single fact is what put a
    # wrong claim in STAGE1.md for eight days -- three header forms "all
    # returned 200" because all three were measured against tools/list. A gate
    # that certifies on a handshake repeats the error it was built after, and
    # would certify codex, whose Bearer cannot call a tool at all.
    #
    # So a hand that connects and lists but never calls is NOT certified. That
    # is not the same as broken, and the message says so.
    clean = [c for c in ok if c["method"] == "tools/call" and not c.get("rpc_error")]
    errored = [c for c in ok if c["method"] == "tools/call" and c.get("rpc_error")]
    if errored and not clean:
        print("  %-9s FAILED   tools/call returned HTTP 200 wrapping a JSON-RPC ERROR: %s. "
              "The transport worked and the CALL did not — 200 is not success for "
              "JSON-RPC." % (hand, errored[0]["rpc_error"]))
        return FAILED
    called = clean
    if not called:
        tried = [c for c in mine if c["method"] == "tools/call"]
        if tried:
            why = "tools/call was refused: HTTP %s." % ",".join(str(c["code"]) for c in tried)
        elif timed_out:
            why = "The model ran out of time before calling -- raise --timeout."
        else:
            # It finished, saw the tool, and chose not to use it. That is about
            # the model or the tool's NAME under this harness, not the config --
            # and saying "raise --timeout" to a run that ended in 12s sends the
            # reader to the wrong place entirely.
            why = ("The hand FINISHED in %.1fs without calling: it connected, listed, "
                   "and declined. The config is fine; look at the model or the tool "
                   "name this harness exposes." % dt)
        if any(c.get("unexpanded") for c in mine):
            print("  %-9s ⚠ THE HAND SENT ITS ENV REFERENCE UNEXPANDED. The credential "
                  "arrived as the literal string \"${...}\", so every 401 below is "
                  "this hand being unable to expand a reference in that position — "
                  "NOT a wrong or missing key." % "")
        print("  %-9s FAILED   connected and listed, but no tools/call was SERVED in "
              "%.1fs (%d call(s): %s). tools/list needs no auth here, so nothing in "
              "this run exercised the credential. %s"
              % (hand, dt, len(mine), ",".join(methods), why))
        for line in [l for l in tail.strip().splitlines() if l.strip()][-6:]:
            print("           | %s" % line[:160])
        return FAILED

    print("  %-9s VERIFIED tools/call SERVED at HTTP 200 in %.1fs -- %d call(s): %s"
          % (hand, dt, len(mine), ",".join(methods)))
    return VERIFIED


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("hand", nargs="?", choices=mcp_config.SUPPORTED_HANDS)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="proxy to the REAL institute and judge on what it answers "
                         "(the only mode whose FAILED verdicts are trustworthy)")
    ap.add_argument("--server", choices=sorted(SERVERS), default="institute",
                    help="which governed server to certify against; 'gateway' is "
                         "the only one codex can authenticate to")
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    if not a.hand and not a.all:
        ap.error("name a hand or pass --all")

    hands = list(mcp_config.SUPPORTED_HANDS) if a.all else [a.hand]
    if a.all:
        run, why = dispatched_hands()
        if run is None:
            print("  ⚠ could not ask the daemon which hands it runs (%s) -- grading all "
                  "%d supported hands. A FAILED here may be about a hand this node "
                  "never dispatches." % (why, len(hands)))
        else:
            skipped = [h for h in hands if h not in run]
            hands = [h for h in hands if h in run]
            if skipped:
                print("  not dispatched on this node, so not graded: %s"
                      % ", ".join(skipped))
            if not hands:
                print("  the daemon enables no supported hand on this node; nothing to "
                      "verify. That is N/A, not a failure.")
                return NOT_APPLICABLE
    print("  probe binds a LAN address, never loopback")
    print("  server: %s" % a.server)
    print("  mode: %s" % ("LIVE -- recording proxy to the real endpoint" if a.live
                          else "trap -- transport only, verdicts are not auth verdicts"))
    worst = VERIFIED
    for h in hands:
        Trap.hits = []
        rc = verify(h, a.timeout, a.live, a.server)
        if rc == FAILED:
            worst = FAILED
        elif rc == NOT_APPLICABLE and worst == VERIFIED:
            worst = NOT_APPLICABLE
    return worst


if __name__ == "__main__":
    sys.exit(main())
