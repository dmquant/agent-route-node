"""The existing analytics test drives the handler — on an EMPTY window only.

rho 9f9a857b-020b-4788-977e-ffd5a0f77f1d explained the split this estate
measured and could not explain:

    local-fabric   185 mutants, 0 killed by an ASSERTION
    rho            349 mutants, 210 killed by an ASSERTION
    "the cause is not that rho writes better tests: rho's suite DRIVES THE
     HANDLERS and asserts on the served envelope, so a mutant changes a value
     a test reads"

That is a CAUSAL claim, and it is testable here rather than agreeable.
tests/test_empty_population_says_so.py already drives get_task_analytics — so
"drives the handler" was not the missing half. What it drives is the EMPTY
window, which returns from `_empty_analytics` before a single metric is
computed. Every mutant in the populated path changes a value no test reads.

So this file drives the SAME handler over a KNOWN POPULATION and asserts on the
served envelope, arithmetic included. If rho's mechanism is right, the
assertion-kill count for this module moves off zero. If it does not, the
mechanism is wrong and the split needs another explanation — which is why the
number is re-measured after this lands rather than assumed.

THE FIXTURE IS PINNED TO A FIXED INSTANT, not to now. `_ts_range` opens the
window at LOCAL midnight, so rows placed relative to `now` drift across a day
boundary for 8 hours in every 24 — the class this estate published in
7d4ae954-c36e-4e87-bb7a-eebd69fedb42 §4 and hit four times in one hour.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-analytics-pop-")

from app import tasks           # noqa: E402
from app import task_analytics  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


# ── the population ────────────────────────────────────────────────────────────
# A fixed local day. Rows sit at 09:00 and 14:00 local so the hour buckets are
# deterministic and do not depend on the hour the suite runs.
DAY = datetime(2026, 5, 14)
ROWS = [
    # (task_id, agent, phase, hour, elapsed_ms, out_chunks, out_bytes, error)
    ("t1", "claude",   "completed",  9, 100, 2, 200, None),
    ("t2", "claude",   "completed",  9, 300, 3, 300, None),
    ("t3", "claude",   "failed",     9, 500, 1, 100, "boom one"),
    ("t4", "opencode", "completed", 14, 700, 4, 400, None),
    ("t5", "opencode", "failed",    14, 900, 1, 50,  "boom two"),
]

tasks.init_task_tables()        # the schema is not created on import
conn = sqlite3.connect(task_analytics.DB_PATH)
for tid, agent, phase, hour, ms, chunks, nbytes, err in ROWS:
    started = (DAY + timedelta(hours=hour)).timestamp() * 1000
    conn.execute(
        "INSERT INTO task_log (task_id, session_id, agent, prompt, phase, started_at,"
        " elapsed_ms, output_chunks, output_bytes, exit_code, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "s-" + tid, agent, "p", phase, started, ms, chunks, nbytes,
         0 if phase == "completed" else 1, err))
conn.commit()
conn.close()

out = task_analytics.get_task_analytics(date_str=DAY.strftime("%Y-%m-%d"), days=1)
m = out["metrics"]

print("populated window: %s, %d rows" % (DAY.strftime("%Y-%m-%d"), len(ROWS)))

# ── the envelope, asserted by value ───────────────────────────────────────────
check("total_tasks counts every row", m["total_tasks"] == 5, str(m["total_tasks"]))
check("success_count counts only completed", m["success_count"] == 3, str(m["success_count"]))
check("error_count counts only failed", m["error_count"] == 2, str(m["error_count"]))
check("success_rate is 3/5 as a percentage", m["success_rate"] == 60.0, str(m["success_rate"]))

# LATENCY IS COMPUTED OVER COMPLETED TASKS ONLY, and the fixture was written
# expecting all five. The code is right and the expectation was wrong: a failed
# task's elapsed time is not a latency. Asserted as the code means it, so the
# fixture's failed rows (500 and 900 ms) are EXCLUDED — and that exclusion is
# itself now under test, because including them would move every number here.
check("avg_latency_ms is the mean of COMPLETED only (100,300,700)",
      m["avg_latency_ms"] == 367, str(m["avg_latency_ms"]))
check("min_latency_ms is the smallest COMPLETED sample", m["min_latency_ms"] == 100,
      str(m["min_latency_ms"]))
check("max_latency_ms is 700, not the failed row's 900",
      m["max_latency_ms"] == 700, str(m["max_latency_ms"]))
check("p50 is the middle completed sample", m["p50_latency_ms"] == 300,
      str(m["p50_latency_ms"]))
check("unique_agents counts distinct agents", m["unique_agents"] == 2,
      str(m["unique_agents"]))
check("total_output_bytes sums every row, failed included",
      m["total_output_bytes"] == 1050, str(m["total_output_bytes"]))

check("measured.metrics_are_meaningful is true on a real population",
      out["measured"]["metrics_are_meaningful"] is True)
check("measured.population equals the row count",
      out["measured"]["population"] == 5, str(out["measured"]["population"]))

# ── per-agent breakdown ───────────────────────────────────────────────────────
ab = out["agent_breakdown"]
check("both agents appear", set(ab) == {"claude", "opencode"}, str(sorted(ab)))
check("claude total is 3", ab["claude"]["total"] == 3, str(ab["claude"]["total"]))
check("claude success_rate is 2/3", round(ab["claude"]["success_rate"], 1) == 66.7,
      str(ab["claude"]["success_rate"]))
check("claude avg latency is the mean of its COMPLETED rows (100,300)",
      ab["claude"]["avg_latency_ms"] == 200, str(ab["claude"]["avg_latency_ms"]))
check("claude max latency is 300, not the failed row's 500",
      ab["claude"]["max_latency_ms"] == 300, str(ab["claude"]["max_latency_ms"]))
check("opencode output_bytes sums its rows", ab["opencode"]["output_bytes"] == 450,
      str(ab["opencode"]["output_bytes"]))
check("opencode output_chunks sums its rows", ab["opencode"]["output_chunks"] == 5,
      str(ab["opencode"]["output_chunks"]))
check("a failed row contributes an error string",
      any("boom" in e.get("error", "") for e in ab["claude"]["recent_errors"]),
      str(ab["claude"]["recent_errors"])[:80])

# ── hourly heatmap: 24 buckets, and only two of them populated ────────────────
hd = {h["hour"]: h for h in out["hourly_heatmap"]}
check("hourly_heatmap has all 24 buckets", len(out["hourly_heatmap"]) == 24,
      str(len(out["hourly_heatmap"])))
check("09:00 holds three tasks", hd["09:00"]["total"] == 3, str(hd["09:00"]))
check("09:00 splits 2 success / 1 failed",
      (hd["09:00"]["success"], hd["09:00"]["failed"]) == (2, 1), str(hd["09:00"]))
check("14:00 splits 1 success / 1 failed",
      (hd["14:00"]["success"], hd["14:00"]["failed"]) == (1, 1), str(hd["14:00"]))
check("an unpopulated bucket is zero, not missing", hd["03:00"]["total"] == 0)

# ── the label this estate added, still present on a populated window ──────────
db = {d["date"]: d for d in out["daily_breakdown"]}
check("daily_breakdown has exactly the populated day", list(db) == ["2026-05-14"], str(list(db)))
check("the day splits 3 success / 2 failed",
      (db["2026-05-14"]["success"], db["2026-05-14"]["failed"]) == (3, 2), str(db["2026-05-14"]))
check("top_sessions is populated", len(out["top_sessions"]) >= 1, str(len(out["top_sessions"])))
check("recent_errors carries both failures", len(out["recent_errors"]) == 2,
      str(len(out["recent_errors"])))

check("time_basis travels with a populated envelope too",
      out["time_basis"]["utc_offset_seconds"] ==
      int(datetime.now().astimezone().utcoffset().total_seconds()))

print()
if fails:
    print("FAILED: %d" % len(fails))
    for f in fails:
        print("   %s" % f)
    raise SystemExit(1)
print("all checks passed on a populated window")
