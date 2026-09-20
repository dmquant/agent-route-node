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
import http.server
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
PROMPT = ('Call the ai-institute MCP tool sessions_get_files with '
          'session_id "verify-probe" and print its raw result. '
          'Call the tool. Do not read any local file and do not answer from memory.')

HAND_CMD = {
    "opencode": lambda cmd, args, p: [cmd, "run", *args, p],
    "claude":   lambda cmd, args, p: [cmd, "@anthropic-ai/claude-code", "-p",
                                      "--dangerously-skip-permissions", *args, p],
    "codex":    lambda cmd, args, p: [cmd, "codex", "exec", "--skip-git-repo-check",
                                      "--dangerously-bypass-approvals-and-sandbox", *args, p],
    "agy":      lambda cmd, args, p: [cmd, *args, p],
}
HAND_BIN = {"opencode": "opencode", "claude": "npx", "codex": "npx", "agy": "agy"}


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
                import json as _j
                name = _j.loads(raw or b"{}").get("method") or "-"
            except Exception:
                name = "-"

            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in DROP_REQ}
            # Cloudflare answers 403 error 1010 to an unusual User-Agent, which
            # reads as "auth fails everywhere" on every arm of the test.
            fwd["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
            authed = any(k.lower() in ("authorization", "x-admin-token")
                         for k in fwd)

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
                                "code": code, "authed": authed})
            self.send_response(code)
            for k, v in hdrs.items():
                if k.lower() not in DROP_RES:
                    self.send_header(k, v)
            self.end_headers()
            try:
                while True:
                    chunk = up.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass                          # the hand hung up; that is its right
            finally:
                try:
                    up.close()
                except Exception:
                    pass

        def do_POST(self):
            self._relay("POST")

        def do_GET(self):
            self._relay("GET")

        def do_DELETE(self):
            self._relay("DELETE")

        def log_message(self, *a):
            pass

    return Proxy


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


def verify(hand: str, timeout: int, live: bool) -> int:
    addr = lan_address()
    if not addr:
        print("  %-9s REFUSED  no non-loopback address on this host; a loopback "
              "verify cannot fail and would report a false pass" % hand)
        return FAILED

    upstream = (os.getenv("AI_INSTITUTE_MCP_URL") or "").strip()
    key = (os.getenv("AI_INSTITUTE_MCP_KEY") or os.getenv("NODE_TOKEN") or "").strip()
    if live and not (upstream and key):
        where = ("%s (%d vars read)" % (ENV_PATH, ENV_VARS) if ENV_VARS >= 0
                 else "%s -- WHICH DOES NOT EXIST" % ENV_PATH)
        print("  %-9s REFUSED  --live needs the node's real endpoint and credential; "
              "%s is unset after reading %s"
              % (hand, "AI_INSTITUTE_MCP_URL" if not upstream else
                 "AI_INSTITUTE_MCP_KEY (and NODE_TOKEN)", where))
        return FAILED

    if live:
        handler = make_proxy(upstream)
        recorder = handler.calls
    else:
        handler = Trap
        recorder = Trap.hits

    srv = http.server.ThreadingHTTPServer((addr, 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    probe_url = "http://%s:%d/api/mcp" % (addr, port)

    guard = NodeGlobalConfig(hand)
    guard.__enter__()
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
    overrides = {"AI_INSTITUTE_MCP_URL": probe_url}
    if not live:
        overrides["AI_INSTITUTE_MCP_KEY"] = "nk_verify_probe"
    elif key:
        overrides["AI_INSTITUTE_MCP_KEY"] = key

    # Build the plan through the SAME code path a dispatch uses. If mcp_config
    # declines this hand, that is a real answer, not an error.
    before = os.environ.get("AI_INSTITUTE_MCP_URL")
    os.environ["AI_INSTITUTE_MCP_URL"] = probe_url
    if not live:
        os.environ.setdefault("AI_INSTITUTE_MCP_KEY", "nk_verify_probe")
    try:
        plan = mcp_config.prepare(hand, workspace)
    finally:
        if before is None:
            os.environ.pop("AI_INSTITUTE_MCP_URL", None)
        else:
            os.environ["AI_INSTITUTE_MCP_URL"] = before

    if not plan and not plan.note.startswith("~/"):
        print("  %-9s N/A      %s" % (hand, plan.note or "not configurable on this node"))
        srv.shutdown(); guard.__exit__()
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
        scheme = os.getenv("AI_INSTITUTE_MCP_SCHEME") or ""
        auth = "%s: %s%s" % (mcp_config._resolve_header(), scheme, key)
    code = _control(probe_url, workspace, auth)
    if code != "200" or not recorder:
        print("  %-9s REFUSED  positive control did not fire: curl to %s returned %r "
              "and the probe saw %d request(s). A FAILED verdict here would be "
              "unreadable, so none is given."
              % (hand, probe_url, code, len(recorder)))
        srv.shutdown(); guard.__exit__()
        return FAILED
    control_n = len(recorder)

    cmd = resolve_cli_path(HAND_BIN[hand])
    argv = HAND_CMD[hand](cmd, plan.args, PROMPT)
    spawn_env = get_cli_env(workspace)
    spawn_env.update(overrides)
    spawn_env.update(plan.env)
    if spawn_env.get("PWD") != workspace:
        print("  %-9s REFUSED  PWD is %r, not the workspace. opencode resolves its "
              "project from PWD; a run from the wrong one tests nothing."
              % (hand, spawn_env.get("PWD")))
        srv.shutdown(); guard.__exit__()
        return FAILED

    t0 = time.time()
    tail = ""
    try:
        r = subprocess.run(argv, cwd=workspace, env=spawn_env, timeout=timeout,
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tail = ((r.stderr or b"") + (r.stdout or b"")).decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        # A hit during the run still counts -- but keep the output, because a
        # hand that hangs without dialling says why in it.
        tail = ((e.stderr or b"") + (e.stdout or b"")).decode("utf-8", "replace")
    except FileNotFoundError:
        print("  %-9s N/A      binary not installed on this node (%s)" % (hand, HAND_BIN[hand]))
        srv.shutdown(); guard.__exit__()
        return NOT_APPLICABLE
    finally:
        srv.shutdown()
        guard.__exit__()
        if not guard.unchanged():
            print("  %-9s REFUSED  node-global config was NOT restored; refusing to "
                  "report a result from a run that changed what it inspected" % hand)
            return FAILED

    dt = time.time() - t0
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

    called = [c for c in ok if c["method"] == "tools/call"]
    print("  %-9s VERIFIED %d call(s) to the real institute in %.1fs, %d at HTTP 200 -- %s"
          % (hand, len(mine), dt, len(ok), ",".join(methods)))
    if not called:
        print("  %-9s          note: tools/list succeeded but no tools/call was made. "
              "tools/list needs no auth, so this run did not exercise the credential."
              % "")
    return VERIFIED


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("hand", nargs="?", choices=mcp_config.SUPPORTED_HANDS)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="proxy to the REAL institute and judge on what it answers "
                         "(the only mode whose FAILED verdicts are trustworthy)")
    ap.add_argument("--timeout", type=int, default=90)
    a = ap.parse_args()
    if not a.hand and not a.all:
        ap.error("name a hand or pass --all")

    hands = list(mcp_config.SUPPORTED_HANDS) if a.all else [a.hand]
    print("  probe binds a LAN address, never loopback")
    print("  mode: %s" % ("LIVE -- recording proxy to the real institute" if a.live
                          else "trap -- transport only, verdicts are not auth verdicts"))
    worst = VERIFIED
    for h in hands:
        Trap.hits = []
        rc = verify(h, a.timeout, a.live)
        if rc == FAILED:
            worst = FAILED
        elif rc == NOT_APPLICABLE and worst == VERIFIED:
            worst = NOT_APPLICABLE
    return worst


if __name__ == "__main__":
    sys.exit(main())
