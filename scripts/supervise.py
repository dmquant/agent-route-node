#!/usr/bin/env python3
"""Node supervision check.

Every probe here exists because it failed on 2026-09-09. The endpoint is checked last and counts
for least: /api/agents answered 200 for twenty minutes while every hand was unresolvable, so a
supervisor that trusts it is measuring the server rather than the work.

Exit 0 = healthy. Exit 1 = something a supervisor should act on. Prints one line per check.
"""
import asyncio, json, os, subprocess, sys, time, urllib.request

NODE = os.path.expanduser("~/.agent-route/node")
sys.path.insert(0, NODE)
fail = []

def check(name, ok, detail=""):
    """Three states, not two.

    OK      the thing is true
    FAIL    the thing is false -- a supervisor should act
    UNKNOWN the check could not run -- NOT a verdict about the host, and not actionable

    Pass ok=None for UNKNOWN. On 2026-09-11 the placement lookup timed out and this reported FAIL,
    waking a supervisor about a node that was healthy and carrying load 3/3. A check that cannot
    separate "false" from "could not ask" is the same defect this file exists to catch.
    """
    state = "OK  " if ok is True else ("FAIL" if ok is False else "????")
    print("  %-22s %s  %s" % (name, state, detail))
    if ok is False:
        fail.append(name)

# 1. the daemon exists. nothing restarts it if it does not.
pids = subprocess.run(["pgrep", "-f", "uvicorn app.main:app --host 0.0.0.0 --port 8017"],
                      capture_output=True, text=True).stdout.split()
check("daemon", bool(pids), "pid " + (pids[0] if pids else "none"))

# 2. the CLI resolves WITHOUT relying on PATH -- the 2026-09-09 outage
try:
    from app.hands.base import resolve_cli_path
    p = resolve_cli_path("opencode")
    check("opencode resolvable", bool(p), p or "not found")
except Exception as e:
    check("opencode resolvable", False, repr(e)[:60])

# 3. the hand itself, not the endpoint
try:
    from app.hands.opencode_hand import OpencodeHand
    check("hand health_check", asyncio.run(OpencodeHand().health_check()))
except Exception as e:
    check("hand health_check", False, repr(e)[:60])

# 4. placement state -- a healthy node excluded from work is still a failure
try:
    env = dict(l.strip().split("=", 1) for l in open(os.path.expanduser("~/.agent-route/.env"))
               if "=" in l and not l.startswith("#"))
    # The worker rejects urllib's default User-Agent with 403; curl gets 200 with any UA.
    req = urllib.request.Request(env["CF_WORKER_URL"] + "/api/nodes",
                                 headers={"X-API-Key": env["NODE_TOKEN"],
                                          "User-Agent": "agent-route-node/supervise"})
    # Identity is inconsistent across this fleet: NODE_ID is the literal name on one host and a
    # UUID on the others, NODE_NAME is wrong on two, and hostname is "Mac" everywhere. Match the
    # id against both the worker's id and its name, which covers all three without trusting
    # NODE_NAME.
    me = env.get("NODE_ID", "").strip('"').strip()
    row = next((n for n in json.load(urllib.request.urlopen(req, timeout=15))["nodes"]
                if n.get("id") == me or n.get("name") == me), None)
    if row:
        check("not degraded", not row.get("degraded"),
              "degraded=%s consec=%s load=%s/%s" % (row.get("degraded"), row.get("consecutive_failures"),
                                                    row.get("current_load"), row.get("max_concurrent")))
    else:
        check("not degraded", False, "node row not found for " + (me or "?"))
except Exception as e:
    # one retry: a single timeout against a remote worker is noise, not a finding
    try:
        row = None
        req = urllib.request.Request(env["CF_WORKER_URL"] + "/api/nodes",
                                     headers={"X-API-Key": env["NODE_TOKEN"],
                                              "User-Agent": "agent-route-node/supervise"})
        me = env.get("NODE_ID", "").strip('"').strip()
        row = next((n for n in json.load(urllib.request.urlopen(req, timeout=30))["nodes"]
                    if n.get("id") == me or n.get("name") == me), None)
        check("not degraded", (not row.get("degraded")) if row else None,
              ("degraded=%s consec=%s" % (row.get("degraded"), row.get("consecutive_failures")))
              if row else "node row not found")
    except Exception as e2:
        check("not degraded", None, "could not reach the worker: " + repr(e2)[:44])

# 5. fast-failing tasks: the signature of a broken hand behind a healthy server
try:
    log = "/tmp/agent-route-node.log"
    fast = 0
    if os.path.exists(log) and time.time() - os.path.getmtime(log) < 86400:
        for line in open(log, errors="ignore"):
            if "done (exit=" in line and "exit=0" not in line:
                tail = line.rsplit(",", 1)[-1]
                try:
                    if float(tail.strip().rstrip("s)")) < 2.0:
                        fast += 1
                except ValueError:
                    pass
    check("no fast failures", fast == 0, "%d sub-2s non-zero exits" % fast)
except Exception as e:
    check("no fast failures", False, repr(e)[:60])

print("  ---", "HEALTHY" if not fail else "ATTENTION: " + ", ".join(fail))
sys.exit(1 if fail else 0)
