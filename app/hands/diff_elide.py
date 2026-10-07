"""Collapse the file diffs codex prints into its transcript, one line per diff.

local-fabric 2026-10-07 (agora a095c28d-cbf2-4ce4-b066-095f5baac1c5): after every `apply patch`
codex exec prints the CUMULATIVE diff of the turn, so a file written once and edited N times is
printed N+1 times. In one 7.5 MB brief result, 99.1% of the bytes were `+` lines of 86 diff
blocks over 8 files; the answer was the other 0.6%. The files themselves are in the workspace,
which syncs to R2, so the diff carries nothing a reader lacks, and its size is what got whole
results refused by the completion callback (IKG-275 f75eccd0-4d95-467a-8684-26fb5beb5ab3).

Each block is replaced by a marker that names the file and its size, so the elision is visible.
A diff inside a ``` fence is the model's own writing and is left alone.
"""
import re

_DIFF_START = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_HEADER = ("new file mode", "deleted file mode", "index ", "--- ", "+++ ", "old mode", "new mode",
           "similarity index", "rename from", "rename to", "copy from", "copy to", "Binary files")


_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _in_diff_body(line: str) -> bool:
    return line[:1] in ("+", "-", " ")


def elide_diffs(text: str) -> str:
    out: list[str] = []
    lines = text.split("\n")
    i, fenced = 0, False
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            fenced = not fenced
        m = None if fenced else _DIFF_START.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        path, added, removed = m.group(2), 0, 0
        i += 1
        while i < len(lines) and lines[i].startswith(_HEADER):
            i += 1
        # Each hunk is bounded by its own @@ counts, so a "- bullet" that follows a diff is not
        # swallowed. The counts are an upper bound: the hand drops blank lines, so a blank
        # context line is missing from the stream and the next non-diff line still ends it.
        while i < len(lines):
            h = _HUNK.match(lines[i])
            if not h:
                break
            old_n, new_n = int(h.group(1) or 1), int(h.group(2) or 1)
            i += 1
            while i < len(lines) and (old_n > 0 or new_n > 0) and _in_diff_body(lines[i]):
                c = lines[i][:1]
                if c == "+":
                    added += 1
                    new_n -= 1
                elif c == "-":
                    removed += 1
                    old_n -= 1
                elif c == " ":
                    old_n -= 1
                    new_n -= 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\ No newline"):
                i += 1
        out.append(f"[diff elided: {path}, +{added}/-{removed} lines; the file is in the workspace]")
    return "\n".join(out)
