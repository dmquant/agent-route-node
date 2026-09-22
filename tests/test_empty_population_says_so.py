"""An empty population must not render as a row of zeros.

epistates 3196c208-6f89-4ed6-93d0-d16dcdb7de14 named the class in one line -- a
DEFAULT standing in for a reading that did not happen -- after finding three
instances in its own code by three different routes. Running the same scan here
returned 267 SHAPES across 38 files, which is a shape count and not a defect list.
This is the one that reached a live endpoint.

get_task_analytics serves GET /api/task-analytics. Every metric carries an
`if X else 0`, so an empty window publishes success_rate 0, avg_latency 0 and
min_latency 0 -- a system that fails everything and answers instantly.

The existing fields are deliberately unchanged, because a live endpoint's zeros
are what some consumer already parses. The `measured` block is additive.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-analytics-")

from app import task_analytics                       # noqa: E402

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


from app.tasks import init_task_tables                # noqa: E402

init_task_tables()


def analytics(tasks):
    """Drive the REAL path, schema and all -- because get_task_analytics returns
    early through _empty_analytics before any metric is computed, and a stub that
    replaced the fetch would have skipped the branch this file is about."""
    conn = task_analytics._get_conn()
    conn.execute("DELETE FROM task_log")
    now = int(__import__("time").time() * 1000)
    for i, t in enumerate(tasks):
        conn.execute(
            "INSERT INTO task_log (task_id, session_id, agent, prompt, phase, "
            "started_at, elapsed_ms, output_chunks, output_bytes, exit_code, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (t["task_id"], "s1", t["agent"], "p", t["phase"], now - 1000 + i,
             t["elapsed_ms"], t["output_chunks"], t["output_bytes"], 0, t["error"]))
    conn.commit()
    conn.close()
    return task_analytics.get_task_analytics()


print("an EMPTY population")
out = analytics([])
m, meas = out["metrics"], out["measured"]
check("the old zeros are UNCHANGED -- nothing a consumer parses moved",
      m["success_rate"] == 0 and m["avg_latency_ms"] == 0 and m["min_latency_ms"] == 0)
check("population is stated", meas["population"] == 0)
check("and the figures are declared meaningless", meas["metrics_are_meaningful"] is False)
check("with a note saying which zero this is", "NO TASKS" in (meas["note"] or ""),
      (meas["note"] or "")[:60])
check("min_latency is called undefined rather than fast",
      "undefined rather than fast" in (meas["note"] or ""))

print("\na NON-empty population")
tasks = [
    {"phase": "completed", "agent": "codex", "elapsed_ms": 100, "task_id": "t1",
     "started_at": 1, "output_bytes": 10, "output_chunks": 1, "error": None},
    {"phase": "failed", "agent": "codex", "elapsed_ms": 0, "task_id": "t2",
     "started_at": 2, "output_bytes": 0, "output_chunks": 0, "error": "boom"},
]
out = analytics(tasks)
m, meas = out["metrics"], out["measured"]
check("population is the real count", meas["population"] == 2)
check("the figures are declared meaningful", meas["metrics_are_meaningful"] is True)
check("and the note is empty, because there is nothing to warn about",
      meas["note"] is None)
check("success_rate is computed, not defaulted", m["success_rate"] == 50.0,
      str(m["success_rate"]))
check("latency_samples counts only the timed ones", meas["latency_samples"] == 1,
      str(meas["latency_samples"]))

print("\nthe population's KNOWN EXCLUSION is stated, not left to be discovered")
check("it names what task_manager does not see", "puller-driven" in meas["excludes"],
      meas["excludes"])

print("\nCONTROL -- the distinction must be able to FAIL")
check("an empty and a non-empty population differ on metrics_are_meaningful",
      analytics([])["measured"]["metrics_are_meaningful"]
      is not analytics(tasks)["measured"]["metrics_are_meaningful"])

print("\nNOT FIXED, and stated rather than left implicit:")
print("  · the zeros themselves. Turning a live endpoint's 0 into null on a quiet")
print("    afternoon is the shape epistates declined to ship for the same reason:")
print("    a change whose population cannot tell it happened until it breaks.")
print("  · that task_log holds 1 row on sg01 against 273 executions that day is a")
print("    fact about the PULLER path, not about this module, and is reported")
print("    rather than repaired here.")

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
