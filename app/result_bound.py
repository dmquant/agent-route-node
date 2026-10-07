"""Bound a task result to what the worker's completion callback will accept.

local-fabric 2026-10-07 (ai-institute IKG-275 f75eccd0-4d95-467a-8684-26fb5beb5ab3): the
worker answers 500 to a result of ~10 MB and 413 to ~90 MB+. Every codex result at or above
10.1 MB on gubei12 and sg01 over two days was refused: 26 results, each finished with exit 0,
none delivered. The node posted once and moved on, so the task sat "running" until the
41-minute watchdog called it a hang. It was never a hang.

The cap is measured on the JSON-encoded body, not on characters: httpx encodes with
ensure_ascii, so one CJK character costs six bytes on the wire.

What is cut is said, in the result itself, with the original size and where the full text
is kept. A truncation the reader cannot see is a fabrication.
"""
import json
import os

MAX_RESULT_BYTES = int(os.getenv("MAX_RESULT_BYTES", str(4_000_000)))
# The final answer is at the end of a CLI transcript; keep more of the tail than the head.
_HEAD_SHARE = 0.25


def encoded_size(text: str) -> int:
    return len(json.dumps(text))


def bound_result(text: str, max_bytes: int = MAX_RESULT_BYTES, kept_at: str | None = None) -> tuple[str, bool]:
    """Return (text, truncated). Unchanged when it already fits."""
    if encoded_size(text) <= max_bytes:
        return text, False
    total = len(text)
    budget = max_bytes - 2048  # room for the marker and the JSON quotes
    # Average wire bytes per char of this text decides how many chars fit.
    ratio = encoded_size(text) / max(total, 1)
    keep = int(budget / ratio)
    while True:
        head_n = int(keep * _HEAD_SHARE)
        tail_n = keep - head_n
        marker = (
            f"\n\n[agent-route-node: RESULT TRUNCATED — {total:,} chars total, the middle "
            f"{total - head_n - tail_n:,} chars were dropped to fit the callback limit of "
            f"{max_bytes:,} bytes; kept the first {head_n:,} and the last {tail_n:,}."
            + (f" Full output kept on the node at {kept_at}." if kept_at else "")
            + "]\n\n"
        )
        out = text[:head_n] + marker + text[total - tail_n:]
        if encoded_size(out) <= max_bytes or keep <= 0:
            return out, True
        keep = int(keep * 0.9)


_KEEP_LAST = 20


def keep_full_output(task_id: str, text: str) -> str | None:
    """Write the untruncated result beside the node's logs; return the path, or None.

    Only the last _KEEP_LAST are kept: one of these was 155 MB.
    """
    try:
        d = os.path.join(os.path.expanduser("~/.agent-route"), "oversize")
        os.makedirs(d, exist_ok=True)
        old = sorted((os.path.join(d, n) for n in os.listdir(d)), key=os.path.getmtime)
        for p in old[: max(0, len(old) - (_KEEP_LAST - 1))]:
            os.remove(p)
        path = os.path.join(d, f"{task_id}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path
    except Exception:
        return None
