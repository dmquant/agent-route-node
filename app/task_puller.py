"""
Task Puller — polls the CF Worker for pending tasks assigned to this node,
executes them locally via the hand registry, and posts results back.
"""
import os
import asyncio
import httpx

_puller_task = None
POLL_INTERVAL = 5  # seconds


async def _pull_and_execute():
    """Pull pending tasks and execute them."""
    from app.hands.registry import hand_registry
    from app.config import get_workspaces_dir

    worker_url = os.getenv("CF_WORKER_URL", "")
    node_id = os.getenv("NODE_ID", "")
    auth_key = os.getenv("NODE_TOKEN", "") or os.getenv("CF_WORKER_API_KEY", "")

    if not worker_url or not node_id or not auth_key:
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Pull pending tasks
            resp = await client.get(
                f"{worker_url}/api/nodes/{node_id}/tasks/pending",
                headers={"X-API-Key": auth_key},
            )
            if resp.status_code != 200:
                return

            tasks = resp.json().get("tasks", [])
            if not tasks:
                return

            for task in tasks:
                task_id = task["id"]
                hand_name = task["hand_name"]
                prompt = task["prompt"]
                session_id = task.get("session_id")

                print(f"[task-puller] Executing task {task_id[:8]} ({hand_name})")

                # Get the hand
                hand = hand_registry.get(hand_name)
                if not hand:
                    print(f"[task-puller] No hand for '{hand_name}', skipping")
                    await _post_result(client, worker_url, auth_key, task_id, 1, f"Hand '{hand_name}' not available")
                    continue

                # Execute
                workspace_dir = os.path.join(get_workspaces_dir(), session_id or task_id)
                os.makedirs(workspace_dir, exist_ok=True)

                try:
                    result = await hand.execute(prompt, workspace_dir=workspace_dir)
                    exit_code = result.exit_code
                    output = result.output
                    print(f"[task-puller] Task {task_id[:8]} done (exit={exit_code}, {len(output)} chars)")
                except Exception as e:
                    exit_code = 1
                    output = str(e)
                    print(f"[task-puller] Task {task_id[:8]} failed: {e}")

                # Post result back
                await _post_result(client, worker_url, auth_key, task_id, exit_code, output)

                # Sync workspace files to R2
                from app.workspace_sync import sync_session_now
                await sync_session_now(session_id or task_id)

    except Exception as e:
        if "ConnectError" not in str(type(e)):
            print(f"[task-puller] Error: {e}")


async def _post_result(client, worker_url, auth_key, task_id, exit_code, result_text):
    """Post task result back to CF Worker callback endpoint."""
    try:
        # Use the tasks/complete endpoint (no HMAC needed, uses token auth)
        await client.post(
            f"{worker_url}/api/tasks/{task_id}/complete",
            json={"exitCode": exit_code, "resultText": result_text},
            headers={"X-API-Key": auth_key, "Content-Type": "application/json"},
            timeout=30,
        )
        # Also complete via node-tasks callback for metrics tracking
        await client.post(
            f"{worker_url}/api/nodes/task-complete/{task_id}",
            json={"exitCode": exit_code, "resultText": result_text},
            headers={"X-API-Key": auth_key, "Content-Type": "application/json"},
            timeout=10,
        )
    except Exception as e:
        print(f"[task-puller] Callback failed for {task_id[:8]}: {e}")


async def _poll_loop():
    """Continuously poll for tasks."""
    while True:
        try:
            await _pull_and_execute()
        except Exception as e:
            print(f"[task-puller] Poll error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


def start_task_puller():
    """Start the background task puller."""
    global _puller_task
    _puller_task = asyncio.create_task(_poll_loop())
    print(f"[task-puller] Started (polling every {POLL_INTERVAL}s)")


def stop_task_puller():
    """Stop the task puller."""
    global _puller_task
    if _puller_task:
        _puller_task.cancel()
        _puller_task = None
