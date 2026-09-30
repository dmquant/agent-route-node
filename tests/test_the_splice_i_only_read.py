"""Exercise the four things LFAB-077 claimed by READING the executor.

In fa0d732f-0bf7-4d6f-b45d-090b96c5719f I marked one residual rather than burying
it: that `workflow_runs = 0` on sg01 means the executor never ran rests on READING
create_run and its call sites, not on measuring them. rho 93462d0b's whole packet
is about the difference between a reading that is right and an entitlement to it.

So this runs the executor for real, locally, against an isolated data directory,
with a stub hand at the one seam the executor uses -- and checks the four claims:

  1  create_run WRITES a workflow_runs row      -> so 0 rows means never entered
  2  step 2 receives "## Context from Previous Step" carrying step 1's output
  3  an EMPTY step-1 output DROPS that block entirely and silently
     (failure mode (a): step 2's prompt becomes indistinguishable from a first
      step's, which is what a hand with nothing upstream would also see)
  4  the block is capped -- a long upstream output arrives truncated, unmarked

AGENT_ROUTE_HOME is set before any app import, because 4dcfa71 was the tick where
a test wrote its fixtures into a live node's data directory.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-splice-home-")

from app.hands.registry import hand_registry              # noqa: E402
from app.session_store import create_session              # noqa: E402
from app.workflow_store import create_workflow, create_run, get_run  # noqa: E402
from app import workflow_executor as wfx                  # noqa: E402
from app.workflow_store import init_workflow_tables       # noqa: E402
import app.session_store as _ss                           # noqa: E402

# A fresh data dir has no schema. session_store builds its own on import; the
# workflow tables are created by an explicit initialiser the app calls at start-up.
from app.tasks import init_task_tables                    # noqa: E402

_ss.init_session_db()
init_workflow_tables()
init_task_tables()

fails = []
seen_prompts = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


class _Result:
    """A hand result. Unknown attributes read as None rather than raising --
    the executor reaches for optional fields (image_b64 among them) and a stub
    that raises on one of them fails the STEP, which is how the first draft of
    this test measured one dispatch instead of two."""

    def __init__(self, exit_code=0, output=""):
        self.exit_code = exit_code
        self.output = output
        # The executor branches on `.success`, not on the exit code. A stub whose
        # __getattr__ returns None for it makes every step an error reading
        # "Exit code 0" -- which is how the second draft measured one dispatch.
        self.success = exit_code == 0

    def __getattr__(self, name):
        return None


class _FakeHand:
    """The one seam the executor uses: hand_registry.get_available -> .execute."""

    name = "gemini"
    hand_type = "fake"

    def __init__(self, outputs):
        self._outputs = list(outputs)

    async def execute(self, prompt, workspace_dir=None, on_log=None):
        seen_prompts.append(prompt)
        out = self._outputs.pop(0) if self._outputs else ""
        if on_log and out:
            await on_log(out)
        return _Result(0, out)


def run_two_steps(outputs):
    seen_prompts.clear()
    hand = _FakeHand(outputs)
    hand_registry.get_available = lambda agent, backups=None: hand   # the seam
    wf = create_workflow(
        name="splice-probe",
        steps=[{"id": "s1", "agent": "gemini", "prompt": "FIRST STEP"},
               {"id": "s2", "agent": "gemini", "prompt": "SECOND STEP"}],
    )
    session = create_session(title="splice-probe", agent_type="workflow")
    run = create_run(workflow_id=wf["id"], session_id=session["id"])
    async def _drive():
        # start_workflow SCHEDULES execute_workflow and returns the Task -- awaiting
        # only start_workflow lets asyncio.run() cancel the work it just started,
        # which is how the first draft of this test measured zero dispatches.
        handle = await wfx.workflow_executor.start_workflow(
            run_id=run["id"], workflow=wf, session_id=session["id"], on_log=None)
        if asyncio.isfuture(handle) or asyncio.iscoroutine(handle):
            await handle
        return handle

    asyncio.run(_drive())
    return run["id"]


print("1 — create_run WRITES a row, so 0 rows means the entry point was never reached")
run_id = run_two_steps(["STEP ONE SAID THIS", "ok"])
row = get_run(run_id)
check("the run row exists after create_run", row is not None, str(run_id)[:12])
check("and it is the row start_workflow updated",
      row is not None and row.get("status") in ("completed", "failed", "running"),
      (row or {}).get("status"))

print("\n2 — step 2 receives the previous step's output, prepended")
check("two steps dispatched", len(seen_prompts) == 2, "%d" % len(seen_prompts))
check("step 1 has NO context block", "## Context from Previous Step" not in seen_prompts[0])
check("step 2 HAS the context block", "## Context from Previous Step" in seen_prompts[1])
check("and it carries step 1's actual output", "STEP ONE SAID THIS" in seen_prompts[1])

print("\n3 — an EMPTY step-1 output drops the block ENTIRELY and says nothing")
run_two_steps(["", "ok"])
check("step 2 gets NO context block at all",
      "## Context from Previous Step" not in seen_prompts[1])
check("and nothing marks that a previous step existed",
      "previous step" not in seen_prompts[1].lower(), seen_prompts[1][:60].replace("\n", " "))
# This is failure mode (a) from LFAB-077, demonstrated rather than read: step 2's
# prompt is now indistinguishable from step 1's shape, so a hand cannot tell
# "there was no upstream step" from "the upstream step produced nothing".
check("step 2's prompt is the same SHAPE as a first step's",
      ("## Context" in seen_prompts[0]) == ("## Context" in seen_prompts[1]))

print("\n4 — the block is capped at 8,000 chars, and the cut is unmarked")
run_two_steps(["A" * 9000 + "TAIL-SENTINEL", "ok"])
body = seen_prompts[1]
check("the sentinel past the cap is ABSENT", "TAIL-SENTINEL" not in body)
check("no marker says the text was cut",
      "truncat" not in body.lower() and "…" not in body.split("## Task")[0],
      "%d chars of A" % body.count("A"))

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
