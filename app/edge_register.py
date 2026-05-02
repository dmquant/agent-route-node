"""
Edge Node Registration — registers with the CF Worker,
receives a node token (nk_...) for SSO access.
"""
import os
import asyncio
import platform
from pathlib import Path
import httpx

_heartbeat_task = None


def _get_config():
    return {
        "worker_url": os.getenv("CF_WORKER_URL", ""),
        "worker_api_key": os.getenv("CF_WORKER_API_KEY", ""),
        "node_token": os.getenv("NODE_TOKEN", ""),
        "node_id": os.getenv("NODE_ID", ""),
        "node_name": os.getenv("NODE_NAME", f"{platform.node()} API Bridge"),
        "node_url": os.getenv("NODE_URL", "http://localhost:8017"),
        "node_key": os.getenv("NODE_KEY", os.getenv("ADMIN_API_KEY", "sk_admin_route_2025")),
    }


def _write_env_token(token: str, node_id: str):
    """Write NODE_TOKEN and NODE_ID to the root .env file."""
    from app.config import get_env_path
    env_path = get_env_path()
    lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            lines = f.readlines()

    # Update or append NODE_TOKEN and NODE_ID
    token_found = False
    id_found = False
    for i, line in enumerate(lines):
        if line.startswith('NODE_TOKEN='):
            lines[i] = f'NODE_TOKEN={token}\n'
            token_found = True
        elif line.startswith('NODE_ID='):
            lines[i] = f'NODE_ID={node_id}\n'
            id_found = True

    if not token_found:
        lines.append(f'NODE_TOKEN={token}\n')
    if not id_found:
        lines.append(f'NODE_ID={node_id}\n')

    with open(env_path, 'w') as f:
        f.writelines(lines)
    print(f"[edge-register] Token written to {env_path}")


async def register_with_worker(available_hands: list[dict]):
    """Register this node with the CF Worker. Returns the node token."""
    cfg = _get_config()
    if not cfg["worker_url"]:
        print("[edge-register] CF_WORKER_URL not set — skipping registration")
        return

    # Use existing token for refresh if available
    auth_key = cfg["node_token"] or cfg["worker_api_key"]
    if not auth_key:
        print("[edge-register] No CF_WORKER_API_KEY or NODE_TOKEN — skipping registration")
        return

    body = {
        "name": cfg["node_name"],
        "apiUrl": cfg["node_url"],
        "apiKey": cfg["node_key"],
        "hands": available_hands,
        "platform": {
            "os": platform.system().lower(),
            "arch": platform.machine(),
            "python": platform.python_version(),
        },
    }

    # If we have a node ID, include it for re-registration
    if cfg["node_id"]:
        body["nodeId"] = cfg["node_id"]

    headers = {
        "Content-Type": "application/json",
        "X-API-Key": auth_key,
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Use refresh endpoint if we have a token, register otherwise
            if cfg["node_token"] and cfg["node_token"].startswith("nk_"):
                resp = await client.post(
                    f"{cfg['worker_url']}/api/nodes/register/refresh",
                    json={"hands": available_hands, "apiUrl": cfg["node_url"], "apiKey": cfg["node_key"]},
                    headers=headers,
                )
            else:
                resp = await client.post(
                    f"{cfg['worker_url']}/api/nodes/register",
                    json=body,
                    headers=headers,
                )

            if resp.status_code >= 300:
                print(f"[edge-register] Registration failed: {resp.status_code} {resp.text}")
                return

            data = resp.json()
            print(f"[edge-register] Registered with CF Worker at {cfg['worker_url']}")

            if data.get("token"):
                token = data["token"]
                node_id = data.get("nodeId", cfg["node_id"])
                os.environ["NODE_TOKEN"] = token
                os.environ["NODE_ID"] = node_id
                _write_env_token(token, node_id)
                print(f"[edge-register] Node token: {token[:12]}...")
                print(f"[edge-register] Node ID: {node_id}")

            if data.get("verifiedHands"):
                print(f"[edge-register] Verified hands: {data['verifiedHands']}")
            if data.get("failedHands"):
                for fh in data["failedHands"]:
                    print(f"[edge-register] Hand test failed: {fh['hand']} — {fh.get('error','unknown')}")

    except Exception as e:
        print(f"[edge-register] Registration error: {e}")


async def _heartbeat_loop():
    """Send heartbeat every 30s with load stats."""
    cfg = _get_config()
    worker_url = cfg["worker_url"]
    if not worker_url:
        return

    while True:
        await asyncio.sleep(30)
        try:
            # Refresh config each loop (token may have been set after startup)
            node_id = os.getenv("NODE_ID", cfg["node_id"])
            auth_key = os.getenv("NODE_TOKEN", "") or cfg["worker_api_key"]
            if not node_id or not auth_key:
                continue

            # Collect load stats
            active_tasks = 0
            try:
                from app.tasks import task_manager
                active_tasks = len(task_manager.get_all_status())
            except Exception:
                pass

            # Hand availability snapshot — lets the worker route around
            # rate-limited hands without waiting for a doomed task to fail.
            # Forward-compatible: workers that don't know this field
            # ignore it.
            hand_status = {}
            try:
                from app.hands.registry import hand_registry
                hand_status = hand_registry.status_snapshot()
            except Exception:
                pass

            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{worker_url}/api/nodes/{node_id}/heartbeat",
                    json={"activeTasks": active_tasks, "handStatus": hand_status},
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": auth_key,
                    },
                )
        except Exception:
            pass


def start_heartbeat():
    global _heartbeat_task
    _heartbeat_task = asyncio.create_task(_heartbeat_loop())


def stop_heartbeat():
    global _heartbeat_task
    if _heartbeat_task:
        _heartbeat_task.cancel()
        _heartbeat_task = None
