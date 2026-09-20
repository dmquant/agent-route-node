#!/usr/bin/env python3
"""Does this hand's MCP configuration actually reach the network from a dispatch?

    python3 scripts/verify_hand_mcp.py opencode
    python3 scripts/verify_hand_mcp.py --all

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
from app.hands.base import resolve_cli_path, get_cli_env  # noqa: E402

VERIFIED, FAILED, NOT_APPLICABLE = 0, 1, 2

# What each hand is asked to do. Short, and it must want a tool -- a prompt the
# model can answer from its own head never dials out, and the trap would then
# report a failure that is really a prompt that asked for nothing.
PROMPT = ("List the tools available from the ai-institute MCP server. "
          "Call the server; do not answer from memory.")

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

    def do_POST(self):
        Trap.hits.append((self.path, self.headers.get("User-Agent", "")))
        body = b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *a):
        pass                                  # the caller reports, not the server


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


def verify(hand: str, timeout: int) -> int:
    addr = lan_address()
    if not addr:
        print("  %-9s REFUSED  no non-loopback address on this host; a loopback "
              "verify cannot fail and would report a false pass" % hand)
        return FAILED

    srv = http.server.HTTPServer((addr, 0), Trap)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    trap_url = "http://%s:%d/api/mcp" % (addr, port)

    guard = NodeGlobalConfig(hand)
    guard.__enter__()
    workspace = tempfile.mkdtemp(prefix="verify-%s-" % hand)
    env = dict(os.environ)
    env["AI_INSTITUTE_MCP_URL"] = trap_url
    env.setdefault("AI_INSTITUTE_MCP_KEY", "nk_verify_probe")

    # Build the plan through the SAME code path a dispatch uses. If mcp_config
    # declines this hand, that is a real answer, not an error.
    before = os.environ.get("AI_INSTITUTE_MCP_URL")
    os.environ["AI_INSTITUTE_MCP_URL"] = trap_url
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
    # "No packet arrived" has two causes and the trap cannot tell them apart:
    # the hand never dialled, or nothing on this host could have reached the
    # trap at all. Without a control that FIRES, a FAILED verdict is
    # uninterpretable -- and reporting it anyway is the well-formed zero this
    # whole guard exists to refuse. So prove the trap is reachable from this
    # host first; if it is not, this refuses to grade the hand.
    ctl = subprocess.run(["curl", "-s", "-m", "5", "-o", "/dev/null",
                          "-w", "%{http_code}", "-X", "POST", trap_url],
                         capture_output=True, text=True)
    if ctl.stdout.strip() != "200" or not Trap.hits:
        print("  %-9s REFUSED  positive control did not fire: curl to %s returned %r "
              "and the trap saw %d request(s). A FAILED verdict here would be "
              "unreadable, so none is given."
              % (hand, trap_url, ctl.stdout.strip(), len(Trap.hits)))
        srv.shutdown(); guard.__exit__()
        return FAILED
    control_hits = len(Trap.hits)

    cmd = resolve_cli_path(HAND_BIN[hand])
    argv = HAND_CMD[hand](cmd, plan.args, PROMPT)
    spawn_env = get_cli_env(workspace)
    spawn_env.update(env)
    spawn_env.update(plan.env)

    t0 = time.time()
    try:
        subprocess.run(argv, cwd=workspace, env=spawn_env, timeout=timeout,
                       stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except subprocess.TimeoutExpired:
        pass                                  # a hit during the run still counts
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
    hand_hits = Trap.hits[control_hits:]          # the control's own hit is not the hand's
    if hand_hits:
        path, ua = hand_hits[0]
        print("  %-9s VERIFIED round trip in %.1fs -- %d request(s) from the hand, "
              "first %s (%s)" % (hand, dt, len(hand_hits), path, (ua or "no UA")[:28]))
        return VERIFIED

    print("  %-9s FAILED   control fired but the hand sent nothing to %s in %.1fs. "
          "The trap is reachable from this host, so the hand ran and did not dial: "
          "its config does not apply under dispatch." % (hand, trap_url, dt))
    return FAILED


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("hand", nargs="?", choices=mcp_config.SUPPORTED_HANDS)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--timeout", type=int, default=90)
    a = ap.parse_args()
    if not a.hand and not a.all:
        ap.error("name a hand or pass --all")

    hands = list(mcp_config.SUPPORTED_HANDS) if a.all else [a.hand]
    print("  trap binds a LAN address, never loopback")
    worst = VERIFIED
    for h in hands:
        Trap.hits = []
        rc = verify(h, a.timeout)
        if rc == FAILED:
            worst = FAILED
        elif rc == NOT_APPLICABLE and worst == VERIFIED:
            worst = NOT_APPLICABLE
    return worst


if __name__ == "__main__":
    sys.exit(main())
