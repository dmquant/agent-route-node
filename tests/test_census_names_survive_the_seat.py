"""What the census may and may not conclude when two captures come from two seats.

History, because the contract changed twice in three hours and the reasons are the
point:

  b74a20c  compared raw names and WARNED across seats. Two captures of an
           UNCHANGED gateway reported 57 added / 57 removed; the warning fired and
           named entitlement, when the corruption was naming.
  90c1bd7  normalised names across seats and computed the delta anyway.
  this     epistates 49f62ef2-3ec8-4785-acac-0d3657e3ad23: "a warning must be right
           about WHY; a refusal only has to be right about WHETHER." The delta is
           REFUSED across seats. What is computed instead is a CORRESPONDENCE, with
           its basis stated -- because refusing outright would have cost the room
           the join that closed 0f1dd12d's residual.

A seat is the triple (host, endpoint, key fingerprint). tools/list is scoped to the
KEY, so one host holding two credentials is two seats.
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


def snap(seat, tools, at, key="kf000000", host="g"):
    ns = {}
    for n in tools:
        k = n.split("__")[0] if "__" in n else "(bare)"
        ns[k] = ns.get(k, 0) + 1
    c = {"measured_at": at, "seat": seat, "endpoint_host": host,
         "total": len(tools), "namespaces": ns, "tools": dict(tools)}
    if key is not None:
        c["key_fingerprint"] = key
    return c


def run(now, prev):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        census.report(now, prev)
    return buf.getvalue()


BARE = {"research_projects_list": ["limit"], "chain_facts_page": ["limit"],
        "analyst_goals_get": ["limit"]}
PREF = {"institute__" + k: v for k, v in BARE.items()}

print("A — a delta across seats is REFUSED, not computed and not merely warned about")
out = run(snap("sg01", PREF, "t2", key="aaa"), snap("epistates", BARE, "t1", key="bbb"))
check("refuses", "DELTA REFUSED" in out)
check("no added/removed line is emitted at all", "added:" not in out and "removed:" not in out)
check("says why a subtraction would lie", "movement that did not happen" in out)

print("B — but the CORRESPONDENCE is computed, with its basis stated")
check("correspondence emitted", "CORRESPONDENCE" in out)
check("basis is stated as an assumption", "ASSUMES" in out and "__" in out)
check("all three matched", "matched      3" in out,
      "; ".join(l.strip() for l in out.splitlines() if "matched" in l))
check("names the pairing", "research_projects_list  <->  institute__research_projects_list" in out)
check("refuses to call a one-sided name a change",
      "ENTITLEMENT difference OR a change over time" in out)

print("C — the same host under a DIFFERENT KEY is a different seat")
out = run(snap("sg01", PREF, "t2", key="aaa"), snap("sg01", PREF, "t1", key="bbb"))
check("refused on the fingerprint alone", "DELTA REFUSED" in out)

print("D — the same seat computes a delta, and a rename is a real change there")
out = run(snap("sg01", {"institute__b": ["limit"]}, "t2"),
          snap("sg01", {"institute__a": ["limit"]}, "t1"))
check("added names the new", "added:   institute__b" in out)
check("removed names the old", "removed: institute__a" in out)
check("not normalised away", "DELTA REFUSED" not in out)

print("E — the same seat, unchanged, reports nothing moved")
out = run(snap("sg01", PREF, "t2"), snap("sg01", PREF, "t1"))
check("added none", "added:   none" in out)
check("removed none", "removed: none" in out)

print("F — args moving under a stable name, same seat")
out = run(snap("sg01", {"institute__x": ["limit", "status"]}, "t2"),
          snap("sg01", {"institute__x": ["limit"]}, "t1"))
check("arg change caught", "args changed on institute__x" in out,
      "; ".join(l.strip() for l in out.splitlines() if "args changed" in l))

print("G — an AMBIGUOUS correspondence is named, never silently paired")
out = run(snap("sg01", {"institute__page": ["limit"], "vibe__page": ["limit"]}, "t2", key="aaa"),
          snap("epistates", {"page": ["limit"]}, "t1", key="bbb"))
check("ambiguity reported", "AMBIGUOUS" in out)
check("both candidates named", "institute__page, vibe__page" in out)

print("H — a capture predating key provenance is PRESUMED, never silently 'same'")
out = run(snap("sg01", PREF, "t2", key="aaa"), snap("sg01", PREF, "t1", key=None))
check("says the key is unverified", "UNVERIFIED" in out)
check("says the reading is presumed", "PRESUMED, not measured" in out)
check("still compares, since host and endpoint match", "added:   none" in out)
out = run(snap("sg01", PREF, "t2", key="aaa"), snap("gubei12", PREF, "t1", key=None))
check("but refuses when the host differs too", "DELTA REFUSED" in out)

print("I — a namespace count is a seat reading and the report must say so")
out = run(snap("epistates", BARE, "t1"), None)
check("labelled AS NAMED AT THIS SEAT", "AS NAMED AT THIS SEAT" in out)
check("unprefixed view flagged, not read as a server fact",
      "not evidence that the gateway namespaces nothing" in out)

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
