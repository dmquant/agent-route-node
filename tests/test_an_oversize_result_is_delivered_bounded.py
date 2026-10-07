"""A result too big for the callback is delivered bounded and marked, never refused whole.

ai-institute IKG-275 f75eccd0-4d95-467a-8684-26fb5beb5ab3 (2026-10-07): the morning brief's codex
step "hung to the 41-minute watchdog" twice. It had finished both times, exit 0, in 14 and 18
minutes; its 155 MB and 90 MB results got 413 from tasks/complete, and every codex result of
10.1 MB or more on gubei12 and sg01 over two days got 500. Nothing was retried.
"""
import asyncio
import json

from app import task_puller
from app.result_bound import bound_result, encoded_size

CAP = 4_000_000


class _Client:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append(json)

        class R:
            status_code = 200
        return R()


def test_a_fitting_result_is_untouched():
    text = "final answer\n" * 1000
    assert bound_result(text, CAP) == (text, False)


def test_an_oversize_result_keeps_head_and_tail_and_says_what_was_cut():
    text = "HEAD-MARK" + "x" * 12_000_000 + "THE FINAL ANSWER"
    out, cut = bound_result(text, CAP, kept_at="/node/oversize/t.txt")
    assert cut
    assert encoded_size(out) <= CAP
    assert out.startswith("HEAD-MARK") and out.endswith("THE FINAL ANSWER")
    assert "RESULT TRUNCATED" in out and f"{len(text):,} chars total" in out
    assert "/node/oversize/t.txt" in out


def test_the_cap_is_on_wire_bytes_not_characters():
    # 1.5M CJK chars is 1.5M characters but ~9 MB once JSON-escaped: over the worker's limit
    text = "研究院" * 500_000
    assert len(text) < CAP < encoded_size(text)
    out, cut = bound_result(text, CAP)
    assert cut and encoded_size(out) <= CAP


def test_the_puller_posts_the_bounded_body_and_keeps_the_full_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # never the live ~/.agent-route
    text = "x" * 11_000_000 + "THE FINAL ANSWER"
    c = _Client()
    asyncio.run(task_puller._post_result(c, "https://w", "k", "task-oversize-1", 0, text))
    assert len(c.bodies) == 2
    for b in c.bodies:
        assert len(json.dumps(b)) <= CAP + 200
        assert b["resultText"].endswith("THE FINAL ANSWER")
        assert b["exitCode"] == 0
    kept = tmp_path / ".agent-route" / "oversize" / "task-oversize-1.txt"
    assert kept.read_text() == text


def test_the_puller_posts_a_small_result_as_is(tmp_path, monkeypatch):
    # control: the bound is inert below the cap and writes nothing
    monkeypatch.setenv("HOME", str(tmp_path))
    c = _Client()
    asyncio.run(task_puller._post_result(c, "https://w", "k", "task-small", 0, "done"))
    assert c.bodies[0]["resultText"] == "done"
    assert not (tmp_path / ".agent-route" / "oversize").exists()
