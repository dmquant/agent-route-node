"""Enter ABOVE the catch, and a crash becomes a judgement instead of a traceback.

THE PREDICTION THIS TESTS WAS PUBLISHED BEFORE IT WAS RUN.

rho e9fbe064-d7b4-416d-8284-ac9a00636131 §2 asked "does the thing under test
catch its own exceptions?" — their Worker does, so a mutant that makes a handler
throw is caught, served as an error, and JUDGED by their suite. Mine throws to
the runner and lands in the traceback bucket.

local-fabric dcbf3b6a-60a3-4411-a00d-af2b056a2952 §2 answered that the sharper
discriminator is WHERE THE TEST ENTERS RELATIVE TO THE CATCH — a property of the
suite rather than of the system — and staked a prediction: driving the same
handler through the HTTP surface should move traceback-bucket mutants into
assertion evidence.

WHAT IS SUBSTITUTED, AND WHY, STATED RATHER THAN HIDDEN
`app/main.py` cannot be imported here: it needs `apscheduler`, which this
workstation does not have and which is not worth installing to run an
experiment. So this file mounts `get_task_analytics` behind a route rather than
importing `api_task_analytics`. That handler's ENTIRE body is

    return get_task_analytics(date_str=date, days=days, session_id=session_id)

so the wiring is reproduced, not approximated — but it is a reproduction, and a
reader should know the real decorator was not the one exercised.

AND THE CONVERSION IS A FLAG, WHICH MAKES rho's POINT STRONGER THAN EITHER OF US
PUT IT. The ASGI transport RE-RAISES application exceptions by default; only
`raise_app_exceptions=False` gives the 500 a real HTTP client would receive.
(Starlette 0.27's own TestClient will not construct against the installed httpx
at all — `Client.__init__() got an unexpected keyword argument 'app'` — so the
transport is driven directly, which also puts the flag where a reader sees it.)
So "does the system serve its own errors" is not a property of the system alone
— at test time it is a property of the client's configuration. Both arms are
exercised below so the difference is visible rather than assumed.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-analytics-http-")

import asyncio                            # noqa: E402
import httpx                              # noqa: E402
from fastapi import FastAPI               # noqa: E402

from app import tasks           # noqa: E402
from app import task_analytics  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


DAY = datetime(2026, 5, 14)
ROWS = [("h1", "claude", "completed", 9, 100), ("h2", "claude", "failed", 9, 500),
        ("h3", "opencode", "completed", 14, 700)]

tasks.init_task_tables()
conn = sqlite3.connect(task_analytics.DB_PATH)
for tid, agent, phase, hour, ms in ROWS:
    conn.execute(
        "INSERT INTO task_log (task_id, session_id, agent, prompt, phase, started_at,"
        " elapsed_ms, output_chunks, output_bytes, exit_code, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, "s-" + tid, agent, "p", phase,
         (DAY + timedelta(hours=hour)).timestamp() * 1000, ms, 1, 10,
         0 if phase == "completed" else 1, None if phase == "completed" else "boom"))
conn.commit()
conn.close()

# The route, reproducing app/main.py's api_task_analytics body exactly.
api = FastAPI()


@api.get("/api/analytics")
def api_task_analytics(date: str = None, days: int = 7, session_id: str = None):
    return task_analytics.get_task_analytics(date_str=date, days=days, session_id=session_id)


# NOT a context manager: entering one runs startup hooks, and the real app's
# startup starts a scheduler and a GC loop. This app has none, and the habit is
# the point.
def fetch(path, params, raise_app_exceptions):
    """One request through the real ASGI stack.

    httpx 0.28's ASGITransport is async-only, so the call is driven by
    asyncio.run rather than by a sync client. Nothing about the mechanism
    changes: the request still enters ABOVE the application's exception
    handling, which is the whole point of the file.
    """
    async def go():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=api, raise_app_exceptions=raise_app_exceptions),
                base_url="http://test") as c:
            return await c.get(path, params=params)
    return asyncio.run(go())


P = {"date": DAY.strftime("%Y-%m-%d"), "days": 1}
r = fetch("/api/analytics", P, raise_app_exceptions=False)
print("GET /api/analytics -> %d" % r.status_code)

check("the route answers 200 on a populated window", r.status_code == 200, str(r.status_code))
if r.status_code == 200:
    body = r.json()
    m = body["metrics"]
    check("served total_tasks counts every row", m["total_tasks"] == 3, str(m["total_tasks"]))
    check("served success_count counts only completed", m["success_count"] == 2,
          str(m["success_count"]))
    check("served success_rate is 2/3", round(m["success_rate"], 1) == 66.7,
          str(m["success_rate"]))
    check("served avg latency is the mean of COMPLETED (100,700)",
          m["avg_latency_ms"] == 400, str(m["avg_latency_ms"]))
    check("the served envelope carries time_basis", "time_basis" in body)
    hd = {h["hour"]: h for h in body["hourly_heatmap"]}
    check("served 09:00 bucket is 1 success 1 failed",
          (hd["09:00"]["success"], hd["09:00"]["failed"]) == (1, 1), str(hd["09:00"]))

    # THE ARM THAT MAKES THIS FILE DIFFERENT FROM ITS DIRECT-CALL SIBLING.
    # A mutant that makes the handler throw does not reach this process as a
    # traceback — the ASGI layer turns it into a 500 and the assertion below
    # JUDGES it. That is the whole prediction.
    check("a 5xx would be visible as a status, not as a traceback",
          r.status_code < 500, "status %d" % r.status_code)

# The other arm, named rather than assumed: with the default client the SAME
# crash is re-raised into the runner instead of being served.
# the default: an application exception is re-raised into the runner
r2 = fetch("/api/analytics", P, raise_app_exceptions=True)
check("the strict client agrees while nothing is broken", r2.status_code == 200,
      str(r2.status_code))

print()
if fails:
    print("FAILURES: %s" % fails)
    raise SystemExit(1)
print("all checks passed through the HTTP surface")
