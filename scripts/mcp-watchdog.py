#!/usr/bin/env python3
"""
Stage-1 watchdog: did deploying the MCP code change anything on the nodes that got it?

Stage 1 ships code that is inert unless AI_INSTITUTE_MCP_URL is set, so the claim
under test is narrow and falsifiable: **the smoke nodes behave like the control
nodes.** This compares them on the things a regression would show up in — the
share of tasks that exit non-zero, and how long they take — and prints both sides
rather than a verdict, because a difference here is a prompt to look, not a result.

    scripts/mcp-watchdog.py [--since-hours 24]

Reads NODE_TOKEN and CF_WORKER_URL from the data-dir .env (~/.agent-route/.env),
never the copy in the repo — those two files drift and the repo one has been
wrong before.
"""
import argparse
import json
import os
import statistics as st
import sys
import urllib.request

SMOKE = {"Studio02", "gubei12"}          # nodes carrying the stage-1 code
CONTROL = {"Studio01", "Studio03", "SG", "CN-H100-DeepSeek"}


def env():
    path = os.path.expanduser("~/.agent-route/.env")
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k] = v
    return out


def fetch(url, key, path):
    req = urllib.request.Request(url + path, headers={
        "X-API-Key": key, "User-Agent": "agent-route-node/mcp-watchdog"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-hours", type=float, default=24.0)
    a = ap.parse_args()
    e = env()
    key, url = e.get("NODE_TOKEN", ""), e.get("CF_WORKER_URL", "")
    if not key or not url:
        print("NODE_TOKEN/CF_WORKER_URL missing from ~/.agent-route/.env", file=sys.stderr)
        return 2

    nodes = {n["id"]: n.get("name") for n in fetch(url, key, "/api/nodes").get("nodes", [])}
    tasks = fetch(url, key, "/api/tasks?limit=500").get("tasks", [])
    import time
    cutoff = (time.time() - a.since_hours * 3600) * 1000

    rows = {}
    for t in tasks:
        if (t.get("created_at") or 0) < cutoff:
            continue
        name = nodes.get(t.get("assigned_node_id")) or "(unassigned)"
        if t.get("status") not in ("completed", "failed"):
            continue
        d = rows.setdefault(name, {"n": 0, "bad": 0, "dur": []})
        d["n"] += 1
        if (t.get("exit_code") or 0) != 0:
            d["bad"] += 1
        s, f = t.get("started_at"), t.get("finished_at")
        if s and f and f > s:
            d["dur"].append((f - s) / 1000)

    print(f"window: last {a.since_hours:g}h    smoke = {', '.join(sorted(SMOKE))}")
    print(f"{'node':<22}{'group':<9}{'n':>5}{'non-zero':>10}{'rate':>8}{'med s':>9}{'p90 s':>9}")
    agg = {"smoke": [0, 0], "control": [0, 0]}
    for name in sorted(rows):
        d = rows[name]
        grp = "SMOKE" if name in SMOKE else ("control" if name in CONTROL else "-")
        med = st.median(d["dur"]) if d["dur"] else 0
        p90 = sorted(d["dur"])[int(len(d["dur"]) * 0.9)] if len(d["dur"]) > 2 else med
        print(f"{name:<22}{grp:<9}{d['n']:>5}{d['bad']:>10}{d['bad']/max(d['n'],1):>8.1%}{med:>9.1f}{p90:>9.1f}")
        if grp == "SMOKE":
            agg["smoke"][0] += d["n"]; agg["smoke"][1] += d["bad"]
        elif grp == "control":
            agg["control"][0] += d["n"]; agg["control"][1] += d["bad"]

    print()
    for g in ("smoke", "control"):
        n, bad = agg[g]
        print(f"  {g:<8} n={n:<5} non-zero={bad:<4} {bad/max(n,1):.1%}")
    if agg["smoke"][0] == 0:
        print("\n  NO TASKS ON THE SMOKE NODES YET — this window proves nothing either way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
