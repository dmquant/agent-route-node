"""The name a hand is advertised under, when it is not its own.

pro6000-1 2026-10-05 (local-fabric 363e5e22-9b65-457f-b6cb-b61f5063e5b3): a node that
advertised plain `opencode` received every caller's opencode work, including prompts
it could not satisfy. agent-route places by hand name, so the fence is a name only
callers who chose this node ask for (agent-route ca418443-eaa2-449f-a98f-e762cbdd6c61).

HAND_ADVERTISE_AS="opencode=opencode-local" advertises the local `opencode` hand as
`opencode-local`, runs `opencode-local` tasks on it, and refuses a task for plain
`opencode`: a fenced hand is not reachable under its own name.
"""
import os


def _pairs() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in os.getenv("HAND_ADVERTISE_AS", "").split(","):
        if "=" not in item:
            continue
        local, adv = (s.strip() for s in item.split("=", 1))
        if local and adv:
            out[local] = adv
    return out


def advertised_name(local: str) -> str:
    return _pairs().get(local, local)


def local_hand_for(name: str) -> str | None:
    """The local hand that runs a task addressed to `name`, or None if refused."""
    pairs = _pairs()
    for local, adv in pairs.items():
        if adv == name:
            return local
    if name in pairs:
        return None
    return name
