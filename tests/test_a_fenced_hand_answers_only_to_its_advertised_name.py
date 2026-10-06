"""A fenced hand is advertised, and run, only under the name its callers chose.

pro6000-1 2026-10-05 (local-fabric 363e5e22-9b65-457f-b6cb-b61f5063e5b3): plain
`opencode` placement sent this node prompts it could not satisfy. The fence is a
distinct advertised name (agent-route ca418443-eaa2-449f-a98f-e762cbdd6c61).
"""
import asyncio

from app import hand_names, task_puller


def test_unset_changes_nothing(monkeypatch):
    monkeypatch.delenv("HAND_ADVERTISE_AS", raising=False)
    assert hand_names.advertised_name("opencode") == "opencode"
    assert hand_names.local_hand_for("opencode") == "opencode"


def test_fence_maps_both_ways_and_refuses_the_plain_name(monkeypatch):
    monkeypatch.setenv("HAND_ADVERTISE_AS", "opencode=opencode-local")
    assert hand_names.advertised_name("opencode") == "opencode-local"
    assert hand_names.advertised_name("codex") == "codex"
    assert hand_names.local_hand_for("opencode-local") == "opencode"
    assert hand_names.local_hand_for("opencode") is None
    assert hand_names.local_hand_for("codex") == "codex"


def test_puller_refuses_a_plain_name_task_without_running_it(monkeypatch):
    monkeypatch.setenv("HAND_ADVERTISE_AS", "opencode=opencode-local")
    posted, resolved = [], []

    async def fake_post(client, url, key, task_id, code, output):
        posted.append((task_id, code, output))

    from app.hands.registry import hand_registry
    monkeypatch.setattr(task_puller, "_post_result", fake_post)
    monkeypatch.setattr(hand_registry, "resolve",
                        lambda name, allow_fallback=True: resolved.append(name) or (None, [name]))

    task = {"id": "t-plain-0000", "hand_name": "opencode", "prompt": "x", "session_id": "s"}
    asyncio.run(task_puller._execute_one(task, "http://w", "k"))
    assert resolved == [], "the fenced hand must not even be resolved"
    assert posted and posted[0][1] == 64 and "FENCED" in posted[0][2]

    # control: the advertised name reaches resolution, under the LOCAL name
    posted.clear()
    task = {"id": "t-local-0000", "hand_name": "opencode-local", "prompt": "x", "session_id": "s"}
    asyncio.run(task_puller._execute_one(task, "http://w", "k"))
    assert resolved == ["opencode"]
