"""Per-CLI rate-limit detection.

Each coding CLI surfaces rate-limit / quota errors in a different shape:

  gemini  → "TerminalQuotaError: ... Your quota will reset after 15h26m35s."
  claude  → "Claude AI usage limit reached. Your usage limit will reset at 14:00 (Asia/Singapore)"
  codex   → "rate_limit_exceeded ... Please try again in 47.3s"
  shared  → "429 Too Many Requests" / "Retry-After: N"

`parse_rate_limit(hand_name, output)` runs the right parser for the named
hand (with a generic fallback) and returns either None (not rate-limited)
or a RateLimitInfo with the wait window expressed both as a duration
(`retry_after_s`) and an absolute reset time (`reset_at_unix`).

When a quota signature is detected but no time can be extracted, we fall
back to **5 hours**. This is intentionally generous: the previous default
of 1 hour caused us to re-hit a 15-hour gemini cooldown 14 hours early.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# When a rate-limit signature is detected but the reset time cannot be
# parsed from the output, fall back to this many seconds. Five hours is
# the rolling window used by Claude's free tier and is a safe default for
# all three CLIs — we'd rather sit on a hand a bit too long than re-hit
# an exhausted quota and cement the cooldown.
DEFAULT_FALLBACK_S = 5 * 3600


@dataclass
class RateLimitInfo:
    """Structured rate-limit signal extracted from a CLI's output."""
    hand: str
    retry_after_s: int
    reset_at_unix: int  # absolute epoch seconds when the hand becomes available again
    reason: str         # human-readable, short — used in /api/hands/status


# ─── Generic signatures (used as a backstop) ─────────────────────────────

_QUOTA_KEYWORDS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "429",
    "too many requests",
    "quota",
    "exhausted",
    "usage limit",
    "resource_exhausted",
    "terminalquotaerror",
    "overloaded",
)

# "try again in 30s", "retry in 5 minutes", "Please try again in 47.3 seconds"
_RETRY_DURATION = re.compile(
    r"(?:try again|retry|wait)(?:\s+in)?\s+(\d+(?:\.\d+)?)\s*"
    r"(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\b",
    re.IGNORECASE,
)

# "Retry-After: 120" header value as it sometimes leaks into stderr text
_RETRY_AFTER_HEADER = re.compile(
    r"retry[-_\s]after[:\s]+(\d+(?:\.\d+)?)", re.IGNORECASE,
)


def _seconds_from_unit(value: float, unit: str) -> int:
    u = unit.lower()
    if u.startswith("h"):
        return int(value * 3600)
    if u.startswith("m") and not u.startswith("ms"):
        return int(value * 60)
    return int(value)


# ─── Per-CLI parsers ─────────────────────────────────────────────────────


def _parse_gemini(output: str) -> Optional[RateLimitInfo]:
    """gemini-cli's TerminalQuotaError format.

    Examples:
      "Your quota will reset after 15h26m35s"
      "Your quota will reset after 1h"
      "Your quota will reset after 47m"
      "RESOURCE_EXHAUSTED" (no duration)
    """
    low = output.lower()

    # Primary: "reset after Xh Ym Zs" — at least one component must be present.
    duration_re = re.compile(
        r"reset\s+after\s+"
        r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?",
        re.IGNORECASE,
    )
    for m in duration_re.finditer(output):
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        if h or mi or s:
            wait = h * 3600 + mi * 60 + s
            return RateLimitInfo(
                hand="gemini",
                retry_after_s=wait,
                reset_at_unix=int(time.time()) + wait,
                reason="quota_exhausted",
            )

    if "terminalquotaerror" in low or "resource_exhausted" in low or (
        "exhausted" in low and "quota" in low
    ):
        return RateLimitInfo(
            hand="gemini",
            retry_after_s=DEFAULT_FALLBACK_S,
            reset_at_unix=int(time.time()) + DEFAULT_FALLBACK_S,
            reason="quota_exhausted_no_eta",
        )
    return None


def _parse_claude(output: str) -> Optional[RateLimitInfo]:
    """Claude Code's usage-limit / rate-limit messages.

    Examples:
      "Claude AI usage limit reached. Your usage limit will reset at 14:00 (Asia/Singapore)"
      "rate_limit_error: rate limit exceeded"
      "Anthropic API error: 429"
      "Overloaded"
    """
    low = output.lower()

    # Primary: wall-clock reset "reset at HH:MM (TZ)"
    m = re.search(
        r"reset\s+at\s+(\d{1,2}):(\d{2})(?:\s*\(([^)]+)\))?",
        output,
        re.IGNORECASE,
    )
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        tz_name = m.group(3) or ""
        # Best-effort timezone parse. zoneinfo is std lib in 3.9+.
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name) if tz_name else timezone.utc
        except Exception:
            tz = timezone.utc

        now_local = datetime.now(tz)
        target = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now_local:
            target += timedelta(days=1)  # next day's reset
        wait = max(60, int((target - now_local).total_seconds()))
        return RateLimitInfo(
            hand="claude",
            retry_after_s=wait,
            reset_at_unix=int(target.timestamp()),
            reason="usage_limit_wall_clock",
        )

    # Secondary: duration phrasing ("try again in N")
    m = _RETRY_DURATION.search(output)
    if m and any(k in low for k in ("rate limit", "usage limit", "429", "overloaded", "rate_limit")):
        wait = _seconds_from_unit(float(m.group(1)), m.group(2))
        return RateLimitInfo(
            hand="claude",
            retry_after_s=wait,
            reset_at_unix=int(time.time()) + wait,
            reason="rate_limit_duration",
        )

    if any(k in low for k in ("usage limit", "rate_limit_error", "rate limit exceeded", "overloaded")):
        return RateLimitInfo(
            hand="claude",
            retry_after_s=DEFAULT_FALLBACK_S,
            reset_at_unix=int(time.time()) + DEFAULT_FALLBACK_S,
            reason="rate_limited_no_eta",
        )
    return None


def _parse_codex(output: str) -> Optional[RateLimitInfo]:
    """OpenAI Codex CLI's rate-limit format.

    Examples:
      "rate_limit_exceeded ... Please try again in 47.3s"
      "Please try again in 5m20s"
      "Please retry in 1 hour"
      "Retry-After: 120"
      "429 - Too Many Requests"
    """
    low = output.lower()

    # Compound "5m20s" pattern
    m = re.search(r"try\s+again\s+in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+(?:\.\d+)?)\s*s)?",
                  output, re.IGNORECASE)
    if m and (m.group(1) or m.group(2) or m.group(3)):
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(float(m.group(3) or 0))
        wait = h * 3600 + mi * 60 + s
        if wait > 0:
            return RateLimitInfo(
                hand="codex",
                retry_after_s=wait,
                reset_at_unix=int(time.time()) + wait,
                reason="rate_limit_duration",
            )

    # Single-unit phrasing
    m = _RETRY_DURATION.search(output)
    if m and any(k in low for k in ("rate", "429", "quota", "exceeded", "exhausted")):
        wait = _seconds_from_unit(float(m.group(1)), m.group(2))
        return RateLimitInfo(
            hand="codex",
            retry_after_s=wait,
            reset_at_unix=int(time.time()) + wait,
            reason="rate_limit_duration",
        )

    # Retry-After header leaked into output
    m = _RETRY_AFTER_HEADER.search(output)
    if m:
        wait = int(float(m.group(1)))
        return RateLimitInfo(
            hand="codex",
            retry_after_s=wait,
            reset_at_unix=int(time.time()) + wait,
            reason="retry_after_header",
        )

    if any(k in low for k in ("rate_limit_exceeded", "429", "too many requests", "quota")):
        return RateLimitInfo(
            hand="codex",
            retry_after_s=DEFAULT_FALLBACK_S,
            reset_at_unix=int(time.time()) + DEFAULT_FALLBACK_S,
            reason="rate_limited_no_eta",
        )
    return None


def _parse_vane(output: str) -> Optional[RateLimitInfo]:
    """Vane wrapper-level errors only.

    Vane returns full Markdown search answers on success — those answers
    are user-prose and can contain any keyword (including "rate limit",
    "quota", "429") about totally unrelated topics. To avoid false
    positives we ONLY match the wrapper's own error formatting:

      "Vane API error 429: ..."
      "Vane API error 503: ..."
      "Failed to get Vane providers"
      "No models available from Vane provider"
      "Vane search failed: ..."

    The vane_hand internally retries SearXNG rate-limits up to 3 times
    before surfacing a wrapper error, so by the time we see one of these
    messages the upstream really is unavailable.
    """
    # Only match at the very start of the output to avoid catching the
    # word "Vane" inside a search-result answer.
    head = output.lstrip()[:120]
    if not head.startswith(("Vane API error", "Vane search failed",
                            "Failed to get Vane providers",
                            "No models available from Vane")):
        return None
    # Try to extract an HTTP code; treat 429 / 503 / 504 as transient.
    m = re.search(r"Vane API error (\d{3})", head)
    code = int(m.group(1)) if m else 0
    if code and code not in (429, 502, 503, 504):
        return None  # other 4xx/5xx are not rate-limit shaped
    return RateLimitInfo(
        hand="vane",
        retry_after_s=DEFAULT_FALLBACK_S,
        reset_at_unix=int(time.time()) + DEFAULT_FALLBACK_S,
        reason=f"vane_upstream_{code}" if code else "vane_unavailable",
    )


_PARSERS = {
    "gemini": _parse_gemini,
    "claude": _parse_claude,
    "codex":  _parse_codex,
    "vane":   _parse_vane,
}


def parse_rate_limit(hand_name: str, output: str) -> Optional[RateLimitInfo]:
    """Return RateLimitInfo if `output` looks rate-limited, else None.

    Strictly per-hand: only hands listed in `_PARSERS` are checked.
    There is intentionally NO generic backstop — running a keyword
    sniffer over arbitrary CLI output (search results, prose, code)
    produces false positives at a rate that's worse than just letting
    the next task discover the rate-limit on its own. Hands that need
    rate-limit handling opt in by registering a dedicated parser.
    """
    if not output:
        return None
    parser = _PARSERS.get(hand_name)
    return parser(output) if parser else None
