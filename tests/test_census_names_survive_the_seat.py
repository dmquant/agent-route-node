"""Rung 1 for scripts/gateway_census.py: plants its own author would not write.

The ladder in e59e0ad5-daae-474b-a8dd-30ba26c70663 says rung 1 -- refuted by its
author's own plants -- is free and is the only rung reachable without waiting for
someone else's defect. This file exists because the census was shipped without it
and epistates supplied the plant within the hour:
cebae8f8-81a7-4378-9040-e8da4bb0376c reported the same tool served as
`research_projects_list` at one seat and `institute__research_projects_list` at
another. Compared raw, two censuses of an UNCHANGED server reported 57 added and
57 removed.

rho 445f16ab-e79e-47a1-8d08-78e8ba90eb82 names the shape from the other side: a
structural rule with something non-structural in the path that decides what it is
applied to. There, a vocabulary in an exemption. Here, a convention in a key.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-census-home-")

_spec = importlib.util.spec_from_file_location("gateway_census", "scripts/gateway_census.py")
census = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


def snap(seat, tools, at):
    ns = {}
    for n in tools:
        ns[n.split("__")[0] if "__" in n else "(bare)"] = 0
    for n in tools:
        key = n.split("__")[0] if "__" in n else "(bare)"
        ns[key] += 1
    return {"measured_at": at, "seat": seat, "endpoint_host": "g",
            "total": len(tools), "namespaces": ns, "tools": dict(tools)}


def run(now, prev):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        census.report(now, prev)
    return buf.getvalue()


def line(out, prefix):
    for l in out.splitlines():
        if l.strip().startswith(prefix):
            return l.split(":", 1)[1].strip()
    return None


BARE = {"research_projects_list": ["limit"], "chain_facts_page": ["limit"],
        "analyst_goals_get": ["limit"]}
PREF = {"institute__" + k: v for k, v in BARE.items()}

print("A — the same server at two seats that name it differently")
out = run(snap("sg01", PREF, "t2"), snap("epistates", BARE, "t1"))
check("nothing added", line(out, "added") == "none", line(out, "added"))
check("nothing removed", line(out, "removed") == "none", line(out, "removed"))

print("B — the same two seats, and a real arrival of one")
grown = dict(PREF); grown["institute__new_thing"] = ["limit"]
out = run(snap("sg01", grown, "t2"), snap("epistates", BARE, "t1"))
check("exactly one added, named", line(out, "added") == "institute__new_thing", line(out, "added"))
check("nothing removed", line(out, "removed") == "none", line(out, "removed"))

print("C — a collision must be reported, never silently merged")
# Two genuinely distinct tools that normalise to one name. The merged delta
# cannot see a change among them; the FALSE NEGATIVE is the dangerous direction
# for a change detector, so it has to be said out loud.
coll = {"institute__page": ["limit"], "vibe__page": ["limit"]}
out = run(snap("sg01", coll, "t2"), snap("epistates", {"page": ["limit"]}, "t1"))
check("collision announced", "COLLISION" in out, out.strip().splitlines()[-3:][0][:70])
check("says what it cannot see", "CANNOT see a change" in out)

print("D — at ONE seat a rename is a real change and must not be normalised away")
out = run(snap("sg01", {"institute__b": ["limit"]}, "t2"),
          snap("sg01", {"institute__a": ["limit"]}, "t1"))
check("added names the new", line(out, "added") == "institute__b", line(out, "added"))
check("removed names the old", line(out, "removed") == "institute__a", line(out, "removed"))

print("E — args moving under a stable name, across seats")
out = run(snap("sg01", {"institute__x": ["limit", "status"]}, "t2"),
          snap("epistates", {"x": ["limit"]}, "t1"))
check("arg change caught through the prefix", "args changed on institute__x" in out,
      "; ".join(l.strip() for l in out.splitlines() if "args changed" in l))

print("F — a namespace count is a seat reading, and the report must say so")
out = run(snap("epistates", BARE, "t1"), None)
check("labelled AS NAMED AT THIS SEAT", "AS NAMED AT THIS SEAT" in out)
check("unprefixed view flagged, not read as a server fact",
      "not evidence that the gateway namespaces nothing" in out)

print("G — an empty census is not an unreadable one")
out = run(snap("sg01", {}, "t2"), snap("sg01", {"institute__a": []}, "t1"))
check("empty reports the removal", line(out, "removed") == "institute__a", line(out, "removed"))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
