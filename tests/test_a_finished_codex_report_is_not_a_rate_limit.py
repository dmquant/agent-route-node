"""A codex run that exited 0 is a finished answer, not a rate limit.

agent-route d66f6411-cd2f-4c58-ae8c-962fb9ab89fd: task 489cd9d0 on gubei12
(10-06) produced a 133,220-char report whose closing sources note said a
press-release fetch "returned HTTP 429". The tail matched codex's 429
pattern, the node rewrote the success to exit 429, and codex was cooled for
30 minutes. codex's own capacity errors exit non-zero.
"""
from app.hands.rate_limit import parse_rate_limit

_REPORT = ("# Whiteboard card\n" + "body text. " * 12000 +
           "\n\n## Sources\n- Yahoo press-release open returned HTTP 429; "
           "excluded from report citations.\n")
_REAL_CODEX = "ERROR: rate_limit_exceeded: Rate limit reached. Please try again in 47.3s"


def test_premise_the_report_tail_matches_the_codex_pattern():
    # without the exit code the old behaviour is reproduced: the report is read as a limit
    assert parse_rate_limit("codex", _REPORT) is not None


def test_a_finished_codex_report_is_not_a_rate_limit():
    assert parse_rate_limit("codex", _REPORT, 0) is None


def test_a_real_codex_limit_still_detected_on_nonzero_exit():
    rl = parse_rate_limit("codex", _REAL_CODEX, 1)
    assert rl is not None and rl.hand == "codex"


def test_agy_exit_zero_geo_block_still_detected():
    # agy reports Google's geo-block with exit 0; the guard must not reach it
    out = "agy backend error: FAILED_PRECONDITION User location is not supported for the API use."
    rl = parse_rate_limit("agy", out, 0)
    assert rl is not None and rl.reason == "geo_block"
