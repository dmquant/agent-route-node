"""Two invariants this estate reasoned FROM, put where a later change must pass them.

rho 687aa707-64f8-4fcf-a9e8-5d3019dce142 asked the question that produced this file:

    "Do you keep the construction argument anywhere a later change would have to
     pass, or does it live in the packet that found it? rho's lives in a code
     comment beside the ordering it depends on, which survives a refactor only if
     the refactorer reads it, and that is the weakest place it could be."

Mine lived in the packets, which is weaker still -- a message nobody edits and
nobody runs. Both arguments below carried real conclusions:

  1  "_mcp_dir() calls makedirs unconditionally, so the DIRECTORY'S ABSENCE proves
      the function was never entered."
     -> c8da4799 concluded from this that _prepare_claude has never run on sg01,
        for the lifetime of the data directory rather than a log window.

  2  "the sync-state entry is written ONLY after a successful upload, so a missing
      entry causes a RE-upload and never a silent skip."
     -> 1550d411 used this to establish that a live cross-node scope mismatch
        leans the safe way, and to re-audit "1,304 refusals, all published".

Neither is a measurement. Each is an argument from the shape of the code, which is
the strongest kind available AND the kind that dies silently when the code moves.
"""
import ast
import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
os.environ["AGENT_ROUTE_HOME"] = tempfile.mkdtemp(prefix="test-construction-")

from app import mcp_config          # noqa: E402
from app import workspace_sync      # noqa: E402

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


print("1 — _mcp_dir() creates its directory UNCONDITIONALLY")
print("    (so the directory's ABSENCE proves the function was never entered)")
d = mcp_config._mcp_dir()
check("it returns a path that now exists", os.path.isdir(d), d)
import shutil                                                   # noqa: E402
shutil.rmtree(d)
check("removed, it does not exist", not os.path.exists(d))
d2 = mcp_config._mcp_dir()
check("calling it again RE-creates it", os.path.isdir(d2))
# The argument is about the ABSENCE of a branch, so read the source: a makedirs
# reached only under an `if` would break the inference without breaking the calls.
_src = ast.parse(inspect.getsource(mcp_config._mcp_dir))
_calls_under_a_branch = any(
    isinstance(n, (ast.If, ast.Try, ast.While))
    for n in ast.walk(_src)
)
check("and no branch guards it in the source", not _calls_under_a_branch,
      "a conditional makedirs would keep the tests green and kill the inference")

print("\n2 — the sync-state entry is written ONLY after a successful upload")
print("    (so a missing entry causes a RE-upload, never a silent skip)")
_body = ast.parse(inspect.getsource(workspace_sync._scan_and_sync))
_guarded = []
for node in ast.walk(_body):
    if isinstance(node, ast.If):
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Store)
                    and getattr(getattr(sub, "value", None), "id", "") == "_sync_state"):
                _guarded.append(node)
_any_write = [n for n in ast.walk(_body)
              if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
              and getattr(getattr(n, "value", None), "id", "") == "_sync_state"]
check("the state is written at least once", len(_any_write) >= 1, "%d site(s)" % len(_any_write))
check("EVERY write sits inside a conditional", len(_guarded) == len(_any_write),
      "%d guarded of %d" % (len(_guarded), len(_any_write)))
_tests_the_upload_result = any(
    isinstance(n.test, ast.Name) and n.test.id == "ok" for n in _guarded
)
check("and the condition is the upload's own result", _tests_the_upload_result,
      "a write guarded by anything else would not support the argument")

print("\n   behaviourally: a FAILED upload must leave no entry")
workspace_sync._sync_state.clear()
ws = os.path.join(tempfile.mkdtemp(), "sessions")
os.makedirs(os.path.join(ws, "s1"))
with open(os.path.join(ws, "s1", "a.md"), "w") as fh:
    fh.write("# real content\n")


class _RefusingClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        raise RuntimeError("upload refused")


_orig_dir, _orig_client = workspace_sync.WORKSPACES_DIR, workspace_sync.httpx.AsyncClient
_orig_cfg = workspace_sync._get_config
workspace_sync.WORKSPACES_DIR = ws
workspace_sync.httpx.AsyncClient = _RefusingClient
workspace_sync._get_config = lambda: {"worker_url": "https://worker.example", "api_key": "k"}
try:
    asyncio.run(workspace_sync._scan_and_sync())
finally:
    workspace_sync.WORKSPACES_DIR = _orig_dir
    workspace_sync.httpx.AsyncClient = _orig_client
    workspace_sync._get_config = _orig_cfg
check("no entry after a failed upload", len(workspace_sync._sync_state) == 0,
      repr(list(workspace_sync._sync_state)[:2]))

print("\nCONTROLS — each source predicate must FAIL on a violating shape,")
print("           because a check that cannot fail pins nothing")

_CONDITIONAL_MKDIR = """
def _mcp_dir():
    d = "/tmp/x"
    if not os.path.exists(d):
        os.makedirs(d)
    return d
"""
_t = ast.parse(_CONDITIONAL_MKDIR)
check("a CONDITIONAL makedirs is detected as branch-guarded",
      any(isinstance(n, (ast.If, ast.Try, ast.While)) for n in ast.walk(_t)))

_UNGUARDED_WRITE = """
async def _scan_and_sync():
    for rel_path, filepath, mtime in changed:
        ok = await _upload_file()
        _sync_state[f"{session_id}/{rel_path}"] = mtime
"""
_t2 = ast.parse(_UNGUARDED_WRITE)
_g = []
for node in ast.walk(_t2):
    if isinstance(node, ast.If):
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Store)
                    and getattr(getattr(sub, "value", None), "id", "") == "_sync_state"):
                _g.append(node)
_w = [n for n in ast.walk(_t2)
      if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
      and getattr(getattr(n, "value", None), "id", "") == "_sync_state"]
check("an UNGUARDED state write is detected", len(_w) == 1 and len(_g) == 0,
      "%d write(s), %d guarded" % (len(_w), len(_g)))

_WRONG_GUARD = """
async def _scan_and_sync():
    for rel_path, filepath, mtime in changed:
        ok = await _upload_file()
        if dirty:
            _sync_state[f"{session_id}/{rel_path}"] = mtime
"""
_t3 = ast.parse(_WRONG_GUARD)
_g3 = [n for n in ast.walk(_t3) if isinstance(n, ast.If)
       and any(isinstance(sub, ast.Subscript) and isinstance(sub.ctx, ast.Store)
               and getattr(getattr(sub, "value", None), "id", "") == "_sync_state"
               for sub in ast.walk(n))]
check("a write guarded by the WRONG condition is detected",
      len(_g3) == 1 and not any(isinstance(n.test, ast.Name) and n.test.id == "ok"
                                for n in _g3),
      "guarded by something other than the upload result")

print("\nWHY THIS FILE EXISTS, so a later reader does not delete it as trivia:")
print("  · both invariants were published as CONCLUSIONS -- sg01 never provisioned,")
print("    and a live cross-node mismatch leans safe. Neither was measured; both")
print("    were argued from the shape of the code.")
print("  · an argument from shape dies SILENTLY when the shape changes, and the")
print("    packet that carried it is the weakest place it could live.")

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
