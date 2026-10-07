"""After a 503 or a timeout from tasks/pending, the next poll comes 5-20 s later, at random, once.

agent-route e9148721-13c4-4217-aeee-8cab46b9441b (2026-10-07): the hourly D1 overload returns 503
or times out, and every node re-polled on the same fixed 15 s beat.
"""
import asyncio

import httpx
import pytest

from app import task_puller


class _Resp:
    def __init__(self, code):
        self.status_code = code
        self.text = ""

    def json(self):
        return {"tasks": []}


def _client(result):
    class C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if isinstance(result, Exception):
                raise result
            return _Resp(result)
    return C


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CF_WORKER_URL", "https://w")
    monkeypatch.setenv("NODE_ID", "n")
    monkeypatch.setenv("NODE_TOKEN", "t")
    task_puller._next_delay = None
    return monkeypatch


def _poll(env, result):
    env.setattr(task_puller.httpx, "AsyncClient", _client(result))
    asyncio.run(task_puller._pull_and_execute())


@pytest.mark.parametrize("result", [503, httpx.ReadTimeout("")])
def test_overload_draws_a_jittered_retry_then_the_beat_resumes(env, result):
    _poll(env, result)
    d = task_puller._take_next_delay()
    assert 5.0 <= d <= 20.0 and d != task_puller.POLL_INTERVAL
    assert task_puller._take_next_delay() == task_puller.POLL_INTERVAL


def test_the_retry_delay_is_actually_random(env):
    seen = set()
    for _ in range(20):
        _poll(env, 503)
        seen.add(round(task_puller._take_next_delay(), 3))
    assert len(seen) > 5


@pytest.mark.parametrize("result", [200, 401, 500])
def test_other_outcomes_keep_the_fixed_beat(env, result):
    # control: only overload signals jitter; an auth failure or a 500 does not
    _poll(env, result)
    assert task_puller._take_next_delay() == task_puller.POLL_INTERVAL


def test_the_poll_loop_sleeps_the_jittered_delay(env):
    calls, slept = [], []

    async def pull():
        calls.append(1)
        if len(calls) == 1:
            task_puller._mark_overloaded()

    async def fake_sleep(d):
        slept.append(d)
        if len(slept) == 2:
            raise asyncio.CancelledError

    # a range that excludes the 15 s beat, or a loop that ignores the jitter passes unseen
    env.setattr(task_puller, "OVERLOAD_RETRY_S", (1.0, 2.0))
    env.setattr(task_puller, "_pull_and_execute", pull)
    env.setattr(task_puller.asyncio, "sleep", fake_sleep)
    asyncio.run(task_puller._poll_loop())
    assert 1.0 <= slept[0] <= 2.0 and slept[1] == task_puller.POLL_INTERVAL
