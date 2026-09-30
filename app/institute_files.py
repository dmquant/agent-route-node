"""
Resolve the files a prompt names, before the model ever runs.

The institute's prompts carry an <INSTITUTE_FILES> block naming exactly the files
a task needs. This module fetches those files, writes them into the task
workspace, and -- the part that matters -- **verifies every named file arrived
before the hand is called at all.**

Why this rather than letting the model fetch them over MCP:

  A fetch that fails inside the model's tool loop produces "I could not read
  that", which is the signal that makes a model retry and then go hunting. Four
  tasks spent $3.04 and 25-30 minutes each doing exactly that. A fetch that
  fails HERE costs milliseconds and produces a task failure someone can read.

  It also works identically on every hand. Doing it in the hands meant four
  different configuration surfaces, three different tool namespaces, and codex
  locked out entirely because it can only carry a credential as
  `Authorization: Bearer`, which the institute rejects. The node has no such
  limits: it is an HTTP client with a token that works.

MCP stays configured for the institute's other 46 tools -- chain_graph_stats,
thesis_evidence, portfolio_get and the rest. Those are mid-reasoning lookups
that cannot be pre-staged, because nobody knows in advance what a model will
ask. It is specifically FILE DELIVERY that belongs here.

INERT WITHOUT A BLOCK. A prompt with no <INSTITUTE_FILES> is returned untouched
and nothing is fetched, so every task that runs today runs identically.
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app import mcp_config

_logger = logging.getLogger("institute_files")

BLOCK_RE = re.compile(r"<INSTITUTE_FILES>(.*?)</INSTITUTE_FILES>", re.S)
READ_TOOL = "sessions_workspace_read"

# A filename arrives from a prompt, so it is untrusted input that becomes a path.
_UNSAFE = re.compile(r"(^/)|(^~)|(\.\.)|(^[A-Za-z]:)")


@dataclass
class FileRef:
    session_id: str
    filename: str
    bytes: Optional[int] = None
    why: str = ""


@dataclass
class StagedFile:
    ref: FileRef
    ok: bool = False
    path: str = ""
    status: str = ""
    error: str = ""
    written: int = 0
    refusal_shaped: str = ""       # the body looks like a saved service refusal


@dataclass
class StageResult:
    present: bool = False          # the prompt carried a block
    ok: bool = True                # every named file is on disk
    files: List[StagedFile] = field(default_factory=list)
    prompt: str = ""
    parse_error: str = ""

    #: The grammar parse_block ENFORCES. Published here because it was
    #: enforced and nowhere declared — the caller who writes the block is
    #: another estate, and the penalty for breaking a rule it cannot read is
    #: that the whole task is refused unrun.
    #:
    #: Adopted from rho, which was asked why callers fail more than humans on
    #: the same surface, tested the question against itself, found every
    #: enforced argument bound absent from the schema a caller reads, and
    #: published them. The same question pointed here gives the same answer.
    GRAMMAR = [
        "one JSON object per line; blank lines and lines starting with # are ignored",
        "session_id  REQUIRED, non-empty after trimming",
        "filename    REQUIRED, non-empty after trimming",
        "filename    must NOT start with '/' or '~', must NOT contain '..',"
        " and must NOT start with a drive letter such as 'C:'",
        "bytes       optional",
        "why         optional, free text shown to the model beside the file",
        "a single malformed line refuses the WHOLE task — no file is staged and"
        " the hand is never called",
    ]

    def failure_text(self) -> str:
        """What the task result says when staging fails. Written for a human
        reading a failed task, not for a model to recover from."""
        lines = ["The files this task names could not be staged, so it was not run.",
                 "No model time was spent. This is a delivery failure, not a task failure.", ""]
        if self.parse_error:
            lines.append(f"  <INSTITUTE_FILES> block: {self.parse_error}")
            # State the WHOLE rule, not only the clause that was broken. A
            # caller who trips one of these cannot see any of them from the
            # outside, and telling it which line failed without telling it what
            # a valid line is leaves it guessing at the next attempt.
            lines.append("")
            lines.append("  The block grammar this node enforces:")
            lines.extend("    - " + g for g in self.GRAMMAR)
        for f in self.files:
            if f.ok:
                lines.append(f"  OK        {f.ref.filename}  ({f.written} bytes)")
            else:
                lines.append(f"  {f.status or 'FAILED':<9} {f.ref.filename}  {f.error}")
        return "\n".join(lines)


def settings() -> Optional[dict]:
    """Where to fetch from, or None when unconfigured."""
    url = (os.getenv("AI_INSTITUTE_MCP_URL") or "").strip()
    if not url:
        return None
    key = (os.getenv("AI_INSTITUTE_MCP_KEY") or os.getenv("NODE_TOKEN") or "").strip()
    if not key:
        return None
    # The default lives in mcp_config.DEFAULT_HEADER, where the measurement behind
    # it is recorded. Imported rather than repeated: these two modules read the same
    # env var and once held different defaults, which is how a node ended up with
    # Bearer-configured hands and a correctly-authenticating fetch path at once.
    return {"url": url, "key": key,
            "header": mcp_config._resolve_header(),
            "scheme": os.getenv("AI_INSTITUTE_MCP_SCHEME") or ""}


def parse_block(prompt: str):
    """(refs, error). A malformed line is an error, never a silently dropped file --
    the whole point is that the block is exhaustive."""
    m = BLOCK_RE.search(prompt or "")
    if not m:
        return None, ""
    refs = []
    for i, line in enumerate(m.group(1).splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            d = json.loads(line)
        except Exception as e:
            return [], f"line {i} is not JSON ({e}): {line[:80]}"
        sid, fn = (d.get("session_id") or "").strip(), (d.get("filename") or "").strip()
        if not sid or not fn:
            return [], f"line {i} needs both session_id and filename: {line[:80]}"
        if _UNSAFE.search(fn):
            return [], f"line {i} filename escapes the workspace: {fn[:80]}"
        refs.append(FileRef(sid, fn, d.get("bytes"), (d.get("why") or "").strip()))
    return refs, ""


# The largest body that could plausibly be a refusal rather than a document.
# The one the operator reported was 236 bytes.
#
# NOTE THE UNIT: `text` is decoded, so this cap and every size this module
# prints are CHARACTERS. They coincide with bytes only for pure ASCII. This is
# stated because the same module published a character count as a byte count on
# 2026-09-22 and built a finding on the difference.
_REFUSAL_MAX = 4096

# Members that appear BESIDE an error in a service refusal and essentially never
# in a document that merely has an "error" field.
_REFUSAL_SIBLINGS = ("credential_presented", "refusal", "detail", "code", "status_code")

# An `error` member whose VALUE says nothing went wrong.
#
# Measured 2026-09-21 by sweeping 193,534 files across both nodes: 11 of 393
# hits were payloads shaped {"error": "ok", "code": ...}, which is how several
# upstream APIs spell SUCCESS. Without this the guard declines to publish
# perfectly good responses -- a 2.8% false-positive rate on real traffic, found
# only because the sweep printed the error strings instead of a count.
_NOT_AN_ERROR = {"ok", "success", "succeeded", "none", "null", "0", "no error", "-"}

# Members a pure error report may carry. An object whose members are ALL drawn
# from this set carries nothing but an error, which is what a refusal envelope
# is. A DOCUMENT that merely records an error also carries its payload -- rows,
# data, results, items -- and that is the structural difference.
# Members that carry ACTUAL CONTENT. A body with any of these populated is a
# document reporting a problem, not an envelope carrying nothing.
_PAYLOAD_MEMBERS = ("result", "data", "rows", "items", "records", "pages",
                    "list", "content", "results", "payload")

_ERROR_ONLY_MEMBERS = {
    "error", "message", "msg", "detail", "details", "reason", "refusal",
    "code", "status", "status_code", "statusCode", "credential_presented",
    "success", "ok", "type", "title", "errors", "error_description",
}


# THE VOCABULARY A SAVED-FAILURE LOOKS LIKE WHEN IT IS NOT JSON.
#
# Declared at module scope, and PRINTED by every caller, because it cannot be
# eliminated and a list a reader can see can be argued with. Moving the list to
# where a reader can see it is the only answer this estate has found to its own
# recurring defect — eleven recorded instances of a vocabulary of literals
# hidden inside a structural path.
SAVED_FAILURE_OPENINGS = (
    "Edge:",                      # 23 bytes: "Edge: Too Many Requests"
    "error code:",                # 17 bytes: Cloudflare's "error code: 1015"
    "Client sent",                # "Client sent an HTTP request to an HTTPS server."
    "Too Many Requests", "Rate limit",
    "403 Forbidden", "404 Not Found", "500 Internal",
    "Service Unavailable", "Bad Gateway", "Access denied", "Forbidden",
)

# An HTML or XML body is not a failure by being HTML. It is a failure when the
# page SAYS so, so the markup arms need a second test rather than a prefix.
_MARKUP_OPENINGS = ("<html", "<!doctype", "<?xml")
_MARKUP_FAILURE_WORDS = ("error", "denied", "forbidden", "not found",
                         "unavailable", "too many", "captcha", "rate limit",
                         "bad gateway", "找不到")

# THE TITLE ARM, from rho 774c0d2f-de4b-4360-876b-922a831f827f §1. A branded
# error page opens with a doctype and chrome and states its failure only in its
# <title> (rho's own Vanguard 404: character 75). Run here it found three pages
# the body-word arm had missed — "502 Bad Gateway" twice on gubei12 and once on
# sg01 — so the 271 published in 12f38e7d-ba1b-42fe-a68a-4301d8b753bf was a floor.
_TITLE = __import__("re").compile(r"<title[^>]*>(.*?)</title>", 2 | 16)


_SEG = __import__("re").compile(r"\s+[|\-\u2013\u2014_]\s+|_")
_FAILURE_SEGMENT = __import__("re").compile(
    r"^(?:(?:4\d\d|5\d\d)(?:\s+error)?\s*[-:]?\s*)?"
    r"(?:page\s+)?(?:not\s+found|access\s+denied|forbidden|bad\s+gateway|"
    r"service\s+(?:temporarily\s+)?unavailable|internal\s+server\s+error|"
    r"too\s+many\s+requests|gateway\s+time-?out)$|^(?:4\d\d|5\d\d)$|^error$", 2)
_CJK_FAILURE = ("页面找不到", "页面不存在", "访问被拒绝")


def _title_is_a_failure(title: str) -> bool:
    """A SEGMENT of the title must BE a failure — never merely mention one.

    rho ab01d502-a886-431a-a436-840bbab62cce §2: a census can over-count
    visibly; a GATE that over-blocks destroys research silently. The first form
    of this arm matched a failure word ANYWHERE and fired on four of rho's five
    pinned admitted titles ("404 Error Handling Best Practices | Blog", "Error
    analysis in numerical methods", "How to fix a 502 Bad Gateway | DevBlog",
    "Not Found: The Story of a Lost City"). It lives in the sweep today, so the
    cost was a wrong count — but a predicate written loose for a count is the
    one that gets reused as a gate, so it is written tight once.
    """
    t = " ".join(title.split())
    if any(c in t for c in _CJK_FAILURE) and t[:3].isdigit():
        return True
    for seg in _SEG.split(t):
        if _FAILURE_SEGMENT.match(seg.strip().lower()):
            return True
    return False


def saved_failure(text: str):
    """A one-line description if this body is an upstream failure saved as DATA.

    THE SIBLING refusal_envelope CANNOT HAVE, AND THE POPULATION IS MEASURED.

    refusal_envelope answers "is this a JSON refusal ENVELOPE" and returns None
    for anything that is not JSON. scripts/sweep_refusals.py prints, at the end
    of every run, that a large saved error PAGE is "the same class of defect and
    is outside this detector by construction" — a scope line, and this estate
    has now learned twice that a scope line is a map of where to look.

    Measured 2026-09-23 across both nodes' workspaces, over the 2,387 `.json`
    files that do not parse as JSON:

        271   an upstream failure saved to a `.json` path
              "404 Not Found" (13 B) · "Edge: Too Many Requests" (23 B) ·
              "error code: 1015" (17 B) · "503 Service Temporarily Unavailable"
              (html) · "Access Denied" (html) · <Error><Code>AccessDenied (xml) ·
              "Client sent an HTTP request to an HTTPS server." (48 B) ·
              "HTTP/2 200" headers saved as the body
       1353   YAML/markdown front-matter under a `.json` name — NOT a failure
        554   truncated or malformed JSON
        108   binary blobs under a `.json` name
         84   empty

    EVERY ONE OF THE 271 WAS INSIDE THE SWEEP'S ELIGIBLE POPULATION AND SCORED
    CLEAN: `.json` is in EXT, they are far under the 4 KiB cap, and
    refusal_envelope returns None for each. They were not outside the detector —
    they were counted as examined and passed, which is worse than uncounted.

    Keyed on the OPENING, never on a mention: a report that discusses a 404 is
    not a saved 404, and the tests plant exactly that case.
    """
    if not text:
        return None
    s = text.lstrip()
    if not s:
        return None
    # RAW RESPONSE HEADERS: a failure only when the STATUS is. The first form
    # keyed on the bare "HTTP/" prefix and fired on `HTTP/2 200` and `HTTP/2 302`
    # header dumps — deliberate `curl -i` output, not failures. Found by reading
    # the non-.json hits of the estate-wide count, 2026-09-23.
    _st = __import__("re").match(r"HTTP/[\d.]+\s+(\d{3})", s)
    if _st:
        if int(_st.group(1)) >= 400:
            return ("this file's CONTENT is raw response headers with status %s "
                    "saved as data" % _st.group(1))
        return None
    for m in SAVED_FAILURE_OPENINGS:
        if s.startswith(m):
            return ("this file's CONTENT is an upstream failure saved as data: "
                    "it opens %r" % s[:60].replace("\n", " "))
    low = s[:20].lower()
    if any(low.startswith(m) for m in _MARKUP_OPENINGS):
        t = _TITLE.search(s[:20000])
        if t and _title_is_a_failure(t.group(1)):
            return ("this file's CONTENT is a markup ERROR PAGE saved as data: "
                    "its title says %r" % t.group(1).strip()[:60])
        # The title has had its say above, tightly. Letting the body arm read it
        # again with the loose word list would re-admit every title the segment
        # rule just refused — so it reads the page with the title removed.
        head = _TITLE.sub(" ", s[:20000])[:800].lower()
        if any(w in head for w in _MARKUP_FAILURE_WORDS):
            return ("this file's CONTENT is a markup ERROR PAGE saved as data: "
                    "it opens %r" % s[:60].replace("\n", " "))
    return None


def refusal_envelope(text: str):
    """A one-line description if this body is a service REFUSAL saved as content.

    Reported 2026-09-21 by the operator: a `recent_reports.md` of 236 bytes
    whose first line is `{"error"`. A fetch elsewhere sent no credential, got a
    401 JSON body, and SAVED IT AS THE FILE. The institute then serves it with
    status ok and a byte count, and this node stages it, and the manifest tells
    the hand the file is complete -- which is true, and useless. The hand reads
    an auth error where a report should be.

    THE CHECK IS STRUCTURAL, NOT TEXTUAL, AND THAT IS THE WHOLE DESIGN.

    `85312249-...` drew the obvious rule -- first line starts with `{"error"` --
    and `9ab0c96e-...` named the trap in the same hour: a check whose subject is
    a TEXTUAL PATTERN fires on any text that DISCUSSES the pattern. This estate
    writes documents about 401 bodies. One of them is this docstring, which
    contains the exact string the naive rule matches, and which must not be
    flagged if it is ever staged as a file.

    So: the WHOLE body must parse as a single JSON object -- prose discussing a
    refusal does not -- it must be small, it must carry an error-ish member, and
    it must carry a sibling that belongs to a refusal rather than to a document.
    Four conditions, none of them a substring search.

    It MARKS, it does not refuse. A JSON fixture that genuinely records an error
    is a legitimate file, and the hand is better placed than this node to tell
    the difference -- provided it is told to look, which is the entire point.
    """
    if not text or len(text) > _REFUSAL_MAX:
        return None
    t = text.strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    try:
        d = json.loads(t)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None

    # THIRD DISCRIMINATOR: A BODY THAT DECLARES ITS OWN FAILURE.
    #
    # rho found a shape neither of the other two rules can reach:
    #
    #   {"version":null,"result":null,"success":false,
    #    "message":"<a refusal, in Chinese>","code":9501}
    #
    # It is a FULL envelope -- `version` and `result` are payload members, so
    # the bare rule cannot see it -- and rho's own text rule missed it because
    # that vocabulary was English over a corpus captured mostly in Chinese.
    # This detector has never used a text vocabulary, so it is immune to the
    # language half by construction rather than by care; but three variants of
    # the same shape DID get past it, measured:
    #
    #   {"success":false,"data":null,"msg":"..."}    `msg` is not `message`
    #   {"success":false,"data":null}                no error string at all
    #   {"ok":false,"result":null,"reason":"denied"} `reason` is not `message`
    #
    # rho's discriminator is the VALUE of a success flag, not the member names
    # and not the text, and their rule is adopted verbatim in spirit: an
    # EXPLICIT false, never an absent flag. A missing `success` is not a
    # declared failure, and a refusal is not inferred from silence.
    # AND THE PAYLOAD MUST BE EMPTY, WHICH IS AI-INSTITUTE'S SHARPENING.
    #
    # A false success flag ALONE is not enough. Measured on their corpus:
    # keying on it blocked 34 REAL market payloads to catch 13 errors -- a
    # 2.6:1 false-positive ratio. It is the NULL RESULT beside the false
    # success that proves the envelope carries nothing.
    #
    # Measured on mine: 34 bodies carry a false flag and ALL 34 have a null or
    # absent payload, so this narrowing costs me nothing today. That is a fact
    # about my CORPUS, not about my rule -- the rule was exactly as dangerous
    # as theirs and had simply not met the hazard yet.
    #
    # The asymmetry is why it is adopted anyway, and it is theirs: a census
    # that over-counts is an embarrassment in a room. A GATE that over-blocks
    # destroys research and says nothing -- the blocked payload never reaches
    # the store and no one is told.
    for flag in ("success", "ok", "succeeded", "isSuccess"):
        if d.get(flag) is False:
            if any(d.get(k) not in (None, "", [], {}, 0) for k in _PAYLOAD_MEMBERS):
                return None            # a real payload that reports partial failure
            why = (d.get("message") or d.get("msg") or d.get("error")
                   or d.get("reason") or d.get("detail") or "")
            why = why if isinstance(why, str) else ""
            return ("this file's CONTENT declares its own failure: %r is false in a "
                    "%d-char JSON body%s. The service answered and said it did NOT "
                    "succeed. Do NOT treat this as the document; say it is missing."
                    % (flag, len(text),
                       " — %s=%r" % ("reason", why[:120]) if why else
                       " and carries no reason at all"))

    err = d.get("error") or d.get("message")
    if not isinstance(err, str) or not err:
        return None
    if err.strip().lower() in _NOT_AN_ERROR:
        return None                       # an `error` field that reports success
    siblings = [k for k in _REFUSAL_SIBLINGS if k in d]

    # A BARE REFUSAL HAS NO SIBLINGS, AND IT IS THE OLDER FORM.
    #
    # ai-institute's census keyed on `credential_presented` and missed 548 of
    # 1,124 -- 49% -- because the refusal this federation served before
    # 2026-09-15 is sixty bytes with ONE member:
    #
    #     {"error":"X-Admin-Token or X-API-Key (viewer:read) required"}
    #
    # Mine required a sibling too, so it was blind in exactly the same way, and
    # blind toward the OLDER half of any corpus.
    #
    # The widening must stay structural, because a textual test for "does this
    # error look like a refusal" is the use/mention trap this file already
    # documents. The structural fact is: a refusal envelope carries NOTHING BUT
    # an error report. A document that happens to record an error carries other
    # members -- rows, data, results -- and those are what distinguish them.
    whole_object_is_an_error = all(k in _ERROR_ONLY_MEMBERS for k in d)
    if not siblings and not whole_object_is_an_error:
        return None
    shape = (", also carries " + ", ".join(siblings)) if siblings else \
            " (a BARE envelope: its only members are %s)" % ", ".join(sorted(d))
    return ("this file's CONTENT is a %d-char JSON refusal, not a document — "
            "error=%r%s. A fetch that failed was saved as the "
            "file. Do NOT treat this as the report; say it is missing."
            % (len(text), err[:120], shape))


async def _read_one(client, cfg: dict, ref: FileRef) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": READ_TOOL,
                       "arguments": {"session_id": ref.session_id, "filename": ref.filename}}}
    r = await client.post(cfg["url"], json=body, timeout=45, headers={
        "Content-Type": "application/json",
        cfg["header"]: cfg["scheme"] + cfg["key"]})
    if r.status_code != 200:
        return {"status": "unavailable", "error": f"HTTP {r.status_code} from the institute"}
    d = r.json()
    if "error" in d:
        e = d["error"]
        cat = (e.get("data") or {}).get("category") or "?"
        return {"status": "unavailable", "error": f"{e.get('message')} ({cat})"}
    try:
        return json.loads(d["result"]["content"][0]["text"])
    except Exception as e:
        return {"status": "unavailable", "error": f"unreadable tool result: {e}"}


async def stage(prompt: str, workspace_dir: str) -> StageResult:
    """Fetch every file the prompt names into the workspace and say whether all
    of them arrived. Never raises: a transport failure is a staging failure with
    a readable reason, which is the whole point."""
    refs, err = parse_block(prompt)
    if refs is None:
        return StageResult(present=False, ok=True, prompt=prompt)
    if err:
        return StageResult(present=True, ok=False, prompt=prompt, parse_error=err)

    cfg = settings()
    if not cfg:
        return StageResult(present=True, ok=False, prompt=prompt,
                           parse_error="a block names files but AI_INSTITUTE_MCP_URL is not set on this node")

    import httpx
    out: List[StagedFile] = []
    async with httpx.AsyncClient() as client:
        for ref in refs:
            sf = StagedFile(ref=ref)
            try:
                res = await _read_one(client, cfg, ref)
            except Exception as e:
                sf.status, sf.error = "unavailable", f"{type(e).__name__}: {e}"
                out.append(sf)
                continue
            sf.status = res.get("status") or ("ok" if res.get("content") is not None else "unavailable")
            if sf.status != "ok" or res.get("content") is None:
                sf.error = (res.get("error") or "no content returned")[:300]
                out.append(sf)
                continue
            dest = os.path.join(workspace_dir, ref.filename)
            try:
                os.makedirs(os.path.dirname(dest) or workspace_dir, exist_ok=True)
                with open(dest, "w", encoding="utf-8") as fh:
                    fh.write(res["content"])
                sf.ok, sf.path, sf.written = True, dest, len(res["content"])
                sf.refusal_shaped = refusal_envelope(res["content"]) or ""
                if sf.refusal_shaped:
                    _logger.warning("institute files: %s staged OK but its content "
                                    "is a saved refusal, not a document", ref.filename)
            except Exception as e:
                sf.status, sf.error = "unavailable", f"could not write into the workspace: {e}"
            out.append(sf)

    ok = all(f.ok for f in out)
    new_prompt = _rewrite(prompt, out, workspace_dir) if ok else prompt
    _logger.info("institute files: %d named, %d staged, ok=%s", len(out), sum(f.ok for f in out), ok)
    return StageResult(present=True, ok=ok, files=out, prompt=new_prompt)


def _rewrite(prompt: str, files: List[StagedFile], workspace_dir: str) -> str:
    """Replace the block with what the model actually needs: local paths, and a
    statement that they are complete.

    This is the anti-hallucination half. Every expensive failure measured on this
    fleet began with a model deciding its input was cut short -- an invented
    `[...]` appearing 40 times across 11 of one session's 52 reasoning blocks and
    0 times in the prompt. A short manifest the node has verified is much harder
    to disbelieve than a wall of inlined text, and it says out loud that there is
    nothing to go looking for.
    """
    # SAY WHAT THE LIST IS EXHAUSTIVE *OF*.
    #
    # This block used to read "The list is exhaustive ... Do not search the
    # filesystem for anything else." Both halves were defects of the same kind:
    #
    #   "exhaustive"     is true of what this task REQUESTED and reads as true
    #                    of what the session HOLDS. A model given four files out
    #                    of a thirty-file session was told the list was
    #                    exhaustive, and it was -- at a scope the model had no
    #                    way to see.
    #   "do not search"  forbade the reader from checking the claim. A summary
    #                    whose purpose is to suppress verification has to be
    #                    right every time, and staging can fail.
    #
    # The anti-hallucination force is kept, because it is measured and it works
    # -- an invented "[...]" appeared 40 times across 11 of one session's 52
    # reasoning blocks. What changes is that the completeness claim is now
    # scoped to the thing it is actually true of, and the reader is given
    # somewhere to put a gap instead of being told there cannot be one.
    #
    # SECOND CORRECTION, same block, twenty minutes later. The first fix
    # qualified the HEAD and left the TAIL asserting "searching the filesystem
    # will not produce it". That is not merely unqualified -- it is FALSE:
    # task_puller syncs the session's R2 contents into this same workspace
    # before the hand runs, and STAGE1 records that as the path actually
    # delivering files today. A model told not to look would miss files that
    # are there.
    #
    # Head-qualified, tail-asserted is worse than plainly unqualified, because
    # the author reads the head and believes the block is fixed while the
    # reader acts on the tail. A keyword audit for "exhaustive" passes it.
    n = len(files)
    body = ["# Staged by the node before this task started. Each file below was fetched",
            "# from the institute and verified present.",
            "#",
            "# SCOPE: %d file(s) were named by this task and %d are staged below." % (n, n),
            "#   This is exhaustive of what THIS TASK REQUESTED. It is NOT a listing of",
            "#   everything the session contains.",
            "#",
            "# CONTENTS: each file below is complete -- nothing is truncated or elided,",
            "#   so a short file is short, not cut off.",
            "#",
            "# If you need a file that is NOT listed here, say so in your output -- it",
            "#   was not requested from the institute for this task. Other files may be",
            "#   present in this workspace from the session's own sync; this block does",
            "#   not describe them."]
    for f in files:
        entry = {"filename": f.ref.filename, "path": f.path,
                 "bytes": f.written, "why": f.ref.why}
        if not f.written:
            # A file can stage SUCCESSFULLY and be empty, and then `why` says
            # what it was supposed to contain while the file says nothing.
            #
            # Measured 2026-09-21: both hands tested read the file and reported
            # it empty WITHOUT this, so it is not a correctness fix -- the
            # emptiness is inspectable, which is what makes it safe. It is here
            # because it saved codex 58% of its tokens (3,619 -> 1,506) by not
            # making it open a file to discover there is nothing in it.
            entry["EMPTY"] = "0 bytes — this file staged successfully and has NO content"
        if f.refusal_shaped:
            # Same cell as EMPTY, and worse: a zero-byte file is visibly empty,
            # while this one has a byte count and an author. The manifest above
            # says the file is COMPLETE, which is true of the transfer and says
            # nothing about whether the bytes are the document.
            entry["REFUSAL_SAVED_AS_CONTENT"] = f.refusal_shaped
        body.append(json.dumps(entry, ensure_ascii=False))
    return BLOCK_RE.sub("<INSTITUTE_FILES>\n" + "\n".join(body) + "\n</INSTITUTE_FILES>",
                        prompt, count=1)
