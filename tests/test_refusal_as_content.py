"""A failed fetch saved as the file: does the manifest say so?

Reported by the operator 2026-09-21 (`85312249-0c48-4aa0-9a97-05991829c320`):
a `recent_reports.md` of 236 bytes whose first line is `{"error"`. Something
fetched it with no credential, got a 401 JSON body, and saved that as the file.
The institute serves it with status ok and a byte count; this node stages it;
the manifest tells the hand the file is COMPLETE -- true of the transfer, and
silent about whether the bytes are the document.

THE POINT OF THIS FILE IS THE NEGATIVE CONTROLS.

agora-pm named the trap in the same hour the defect was reported: a check whose
subject is a TEXTUAL PATTERN fires on any text that DISCUSSES the pattern. This
estate writes documents about 401 bodies -- including the docstring of the
function under test, which contains the exact string a naive rule matches. If
the detector cannot tell a refusal from a document about refusals, it is worse
than nothing, because it would flag this repository's own writing.

No network: every payload here is a literal.
"""
import importlib, json, os, sys, tempfile

sys.path.insert(0, os.getcwd())
os.environ.setdefault("AI_INSTITUTE_MCP_URL", "https://institute.example/api/mcp")
os.environ.setdefault("AI_INSTITUTE_MCP_KEY", "nk_institute")

from app import institute_files as F
importlib.reload(F)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


# The operator's payload, verbatim.
OPERATOR = ('{"error":"X-Admin-Token or X-API-Key (viewer:read|analysts:read) required",'
            '"credential_presented":false,"refusal":"absent","detail":"no credential was '
            'presented on this request. Send X-API-Key, Authorization: Bearer, or '
            'X-Admin-Token."}')

print("MUST FIRE — a refusal saved as content")
r = F.refusal_envelope(OPERATOR)
check("the operator's exact 401 body is caught", bool(r), "%d bytes" % len(OPERATOR))
check("the description names the error", bool(r) and "X-Admin-Token" in r)
check("the description tells the hand what to do", bool(r) and "missing" in r)
check("`message`+`code` shape also caught",
      bool(F.refusal_envelope('{"message":"unauthorized","code":401}')))

print("\nMUST NOT FIRE — the use/mention controls")
DOC_ABOUT_401 = """# Auth troubleshooting

When no credential is sent the institute answers 401 with a body like
`{"error":"X-Admin-Token or X-API-Key required","credential_presented":false}`.
Note `refusal: "absent"` distinguishes an absent credential from a wrong one.
"""
check("a markdown doc DISCUSSING a 401 body is not flagged",
      F.refusal_envelope(DOC_ABOUT_401) is None)
check("this detector's OWN docstring is not flagged",
      F.refusal_envelope(F.refusal_envelope.__doc__) is None)
check("this test file's own docstring is not flagged",
      F.refusal_envelope(__doc__) is None)

print("\nMUST NOT FIRE — documents that merely contain an error member")
check("JSON with `error` but no refusal sibling",
      F.refusal_envelope('{"error":"none","rows":[1,2,3]}') is None)
check("a JSON array", F.refusal_envelope('[{"error":"x","detail":"y"}]') is None)
check("empty", F.refusal_envelope("") is None)
check("plain prose", F.refusal_envelope("The report is attached.") is None)

# Found by sweeping real traffic, not by imagining cases: 11 of 393 hits across
# both nodes were {"error":"ok"}, which is how several upstream APIs spell
# SUCCESS. The guard would have declined to publish them.
for ok_val in ("ok", "OK", "success", "none", "0"):
    check("an `error` member saying %r is not a refusal" % ok_val,
          F.refusal_envelope('{"error":"%s","code":200}' % ok_val) is None)
check("a REAL error with a code is still caught",
      bool(F.refusal_envelope('{"error":"RZYE\u6392\u5e8f\u5217\u4e0d\u5b58\u5728","code":-1}')))
big = '{"error":"x","detail":"' + ("y" * 5000) + '"}'
check("a large body is a document, not a refusal", F.refusal_envelope(big) is None,
      "%d bytes" % len(big))

print("\nEND TO END — does the manifest carry the marker?")
ws = tempfile.mkdtemp()
good = os.path.join(ws, "alpha.md")
bad = os.path.join(ws, "recent_reports.md")
open(good, "w").write("# Alpha\n\nReal content.\n")
open(bad, "w").write(OPERATOR)
files = [
    F.StagedFile(ref=F.FileRef("s1", "alpha.md", why="the real one"),
                 ok=True, path=good, written=os.path.getsize(good)),
    F.StagedFile(ref=F.FileRef("s1", "recent_reports.md", why="the analyst reports"),
                 ok=True, path=bad, written=len(OPERATOR),
                 refusal_shaped=F.refusal_envelope(OPERATOR) or ""),
]
block = F._rewrite("<INSTITUTE_FILES>\n{}\n</INSTITUTE_FILES>", files, ws)
lines = [l for l in block.splitlines() if l.startswith("{")]
entries = {json.loads(l)["filename"]: json.loads(l) for l in lines}
check("the refusal file is marked",
      "REFUSAL_SAVED_AS_CONTENT" in entries["recent_reports.md"])
check("the real file is NOT marked",
      "REFUSAL_SAVED_AS_CONTENT" not in entries["alpha.md"])
check("the marked file still carries its byte count and path",
      entries["recent_reports.md"]["bytes"] == len(OPERATOR)
      and entries["recent_reports.md"]["path"] == bad)

print("\nTHE PRODUCER SIDE — the upload must not publish a saved refusal")
import asyncio
from app import workspace_sync

calls = []


class _FakeResp:
    status_code = 200


class _FakeClient:
    async def post(self, url, **kw):
        calls.append(kw.get("json", {}).get("path"))
        return _FakeResp()


async def _upload(name, text):
    d = tempfile.mkdtemp()
    fp = os.path.join(d, name)
    open(fp, "w").write(text)
    return await workspace_sync._upload_file(
        _FakeClient(), "https://worker.example", "k", "s1", name, fp)

check("a real document is still uploaded",
      asyncio.run(_upload("alpha.md", "# Alpha\n\nReal content.\n")) is True)
check("the 401 body is REFUSED, not published",
      asyncio.run(_upload("recent_reports.md", OPERATOR)) is False)
check("only the real file reached the worker", calls == ["alpha.md"], repr(calls))
check("a doc ABOUT a 401 is still uploaded",
      asyncio.run(_upload("auth-notes.md", DOC_ABOUT_401)) is True)

print("\nTHE HAND MUST LEARN WHY — ai-institute's question about the first version")
d = tempfile.mkdtemp()
fp = os.path.join(d, "recent_reports.md")
open(fp, "w").write(OPERATOR)
asyncio.run(workspace_sync._upload_file(
    _FakeClient(), "https://worker.example", "k", "s1", "recent_reports.md", fp))
marker = fp + ".NOT_PUBLISHED.md"
check("a marker is left beside the file", os.path.exists(marker))
mt = open(marker).read() if os.path.exists(marker) else ""
check("it says the file was not published", "NOT published" in mt)
check("it carries the reason", "refusal" in mt.lower())
check("it says the local file was not deleted", "nothing was deleted" in mt)
check("it tells the hand to report the document MISSING", "MISSING" in mt)
check("the original file is untouched", open(fp).read() == OPERATOR)
check("the marker itself would publish normally (it is not a refusal)",
      F.refusal_envelope(mt) is None)

print("\nthe marker must not break the sync when the workspace is read-only")
ro = tempfile.mkdtemp()
rofp = os.path.join(ro, "x.md")
open(rofp, "w").write(OPERATOR)
os.chmod(ro, 0o500)
try:
    r_ok = asyncio.run(workspace_sync._upload_file(
        _FakeClient(), "https://worker.example", "k", "s1", "x.md", rofp)) is False
finally:
    os.chmod(ro, 0o700)
check("still refuses, does not raise, when the marker cannot be written", r_ok)

print("\nUNREADABLE STATE IS NOT EMPTY STATE")
# An unreadable sync-state file used to land on {} silently, which reads as
# "nothing has ever been synced" and re-uploads every tracked file.
sd = tempfile.mkdtemp()
orig_state = workspace_sync.STATE_FILE
try:
    workspace_sync.STATE_FILE = os.path.join(sd, "state.json")
    workspace_sync._load_state()
    check("no state file at all is a clean empty state",
          workspace_sync._sync_state == {} and not workspace_sync._state_was_unreadable)

    open(workspace_sync.STATE_FILE, "w").write('{"a": 1.0}')
    workspace_sync._load_state()
    check("a readable state file loads", workspace_sync._sync_state == {"a": 1.0})
    check("and is not marked unreadable", not workspace_sync._state_was_unreadable)

    open(workspace_sync.STATE_FILE, "w").write("{ this is not json")
    workspace_sync._load_state()
    check("an UNREADABLE state file still yields an empty dict",
          workspace_sync._sync_state == {})
    check("but the caller can tell it apart from having no state",
          bool(workspace_sync._state_was_unreadable),
          workspace_sync._state_was_unreadable[:60])
    check("the unreadable file is not deleted",
          os.path.exists(workspace_sync.STATE_FILE))
finally:
    workspace_sync.STATE_FILE = orig_state

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
sys.exit(1 if fails else 0)
