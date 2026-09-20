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


@dataclass
class StageResult:
    present: bool = False          # the prompt carried a block
    ok: bool = True                # every named file is on disk
    files: List[StagedFile] = field(default_factory=list)
    prompt: str = ""
    parse_error: str = ""

    def failure_text(self) -> str:
        """What the task result says when staging fails. Written for a human
        reading a failed task, not for a model to recover from."""
        lines = ["The files this task names could not be staged, so it was not run.",
                 "No model time was spent. This is a delivery failure, not a task failure.", ""]
        if self.parse_error:
            lines.append(f"  <INSTITUTE_FILES> block: {self.parse_error}")
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
        body.append(json.dumps(entry, ensure_ascii=False))
    return BLOCK_RE.sub("<INSTITUTE_FILES>\n" + "\n".join(body) + "\n</INSTITUTE_FILES>",
                        prompt, count=1)
