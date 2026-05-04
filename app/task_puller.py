"""
Task Puller — polls the CF Worker for pending tasks assigned to this node,
executes them locally via the hand registry, and posts results back.

Concurrent execution model:
  - The poll loop fires every POLL_INTERVAL seconds and pulls up to 5 pending
    tasks per call. Each task spawns as its own asyncio coroutine, gated by a
    global semaphore sized to MAX_CONCURRENT.
  - The poll loop never waits for a task to finish — it returns as soon as
    coroutines are spawned, so a single slow Claude/Codex CLI can no longer
    wedge the entire node.
  - In-flight task count is tracked so we don't pull more than we can
    accommodate (= MAX_CONCURRENT - currently_running).

Logs all activity to ~/.agent-route/task_puller.log
"""
from __future__ import annotations  # PEP 604 union syntax for Python 3.9 venvs

import os
import asyncio
import logging
from datetime import datetime
import httpx

from app.config import get_data_dir

_puller_task = None

# Tunables. POLL_INTERVAL is short because the loop is now non-blocking; the
# old 60s value was a workaround for the sequential bottleneck.
POLL_INTERVAL = 15  # seconds
MAX_CONCURRENT = int(os.getenv("NODE_MAX_CONCURRENT", "3"))

# Concurrency primitives. Created lazily on first poll so import order is safe.
_executor_sem: asyncio.Semaphore | None = None
_inflight_ids: set[str] = set()


def get_inflight_count() -> int:
    """Number of tasks the puller is currently executing.

    Public accessor so the heartbeat (edge_register.py) can report a load
    figure that includes puller-driven work. The puller has its own
    concurrency tracking separate from app.tasks.task_manager (which only
    counts direct /execute and workflow calls), so without this the
    worker's edge_nodes.current_load shows 0/3 even when the puller is
    saturated.
    """
    return len(_inflight_ids)

# File logger
_log_file = os.path.join(get_data_dir(), "task_puller.log")
_logger = logging.getLogger("task_puller")
_logger.setLevel(logging.DEBUG)
_fh = logging.FileHandler(_log_file, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_logger.addHandler(_fh)


def _ensure_sem() -> asyncio.Semaphore:
    """Create the semaphore on first use (after the event loop exists)."""
    global _executor_sem
    if _executor_sem is None:
        _executor_sem = asyncio.Semaphore(MAX_CONCURRENT)
    return _executor_sem


async def _execute_one(task: dict, worker_url: str, auth_key: str) -> None:
    """Execute a single task end-to-end — gated by the concurrency semaphore.

    Spawned as a fire-and-forget asyncio task by `_pull_and_execute`. Owns
    its own httpx client so the poll loop's client can be closed
    independently. Always removes itself from `_inflight_ids` on exit so a
    crashed task can't leak a slot.

    Fallback semantics: if the requested hand is in cooldown (or not
    enabled, or unregistered), we transparently route to a hand from
    `DEFAULT_FALLBACK_CHAINS`. Set `task['meta']['fallback'] = false` on
    the worker side to opt out — the original hand will be used and the
    task will fail-fast if that hand is unavailable.
    """
    from app.hands.registry import hand_registry, DEFAULT_FALLBACK_CHAINS
    from app.hands.rate_limit import parse_rate_limit
    from app.config import get_workspaces_dir

    task_id = task["id"]
    hand_name = task["hand_name"]
    prompt = task["prompt"]
    session_id = task.get("session_id")
    # Optional per-task knobs. Worker may forward these in `meta` or as
    # top-level fields — accept both for forward-compat.
    meta = task.get("meta") or {}
    requested_model = task.get("model") or meta.get("model")
    allow_fallback = task.get("fallback", meta.get("fallback", True))
    if isinstance(allow_fallback, str):
        allow_fallback = allow_fallback.lower() not in ("false", "0", "no")

    sem = _ensure_sem()
    async with sem:
        start_time = datetime.now()
        _logger.info(f"EXEC {task_id[:12]} | hand={hand_name} | model={requested_model or '(default)'} | prompt={prompt[:80]}...")
        print(f"[task-puller] Executing {task_id[:8]} ({hand_name})")

        exit_code = 1
        output = ""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Resolve the actual hand to run, applying cooldown +
                # fallback chain. `tried` records every hand we examined.
                hand, tried = hand_registry.resolve(hand_name, allow_fallback=allow_fallback)
                if not hand:
                    # Either nothing registered under that name OR everything
                    # in the fallback chain is on cooldown. Surface a
                    # structured 429-style result so the worker can decide
                    # whether to re-queue or surface to the user.
                    cd_summary = _cooldown_summary(hand_registry, [hand_name] + DEFAULT_FALLBACK_CHAINS.get(hand_name, []))
                    output = (
                        f"[RATE_LIMITED hand={hand_name} all_exhausted=true tried={','.join(tried)}]\n"
                        f"All candidate hands are on cooldown.\n{cd_summary}"
                    )
                    _logger.warning(f"BLOCKED {task_id[:12]} | {output[:200]}")
                    print(f"[task-puller] Task {task_id[:8]} blocked — all hands cool")
                    await _post_result(client, worker_url, auth_key, task_id, 429, output)
                    return

                if hand.name != hand_name:
                    print(f"[task-puller] {hand_name} unavailable, executing on {hand.name} instead")

                workspace_dir = os.path.join(get_workspaces_dir(), session_id or task_id)
                os.makedirs(workspace_dir, exist_ok=True)

                # Pull workspace files generated by other nodes from R2.
                await _sync_workspace_from_r2(client, worker_url, auth_key, session_id or task_id, workspace_dir)

                try:
                    exec_kwargs = {"workspace_dir": workspace_dir}
                    if requested_model:
                        exec_kwargs["model"] = requested_model
                    result = await hand.execute(prompt, **exec_kwargs)
                    exit_code = result.exit_code
                    output = result.output
                    elapsed = (datetime.now() - start_time).total_seconds()
                    _logger.info(f"DONE {task_id[:12]} | hand={hand.name} | exit={exit_code} | {elapsed:.1f}s | {len(output)} chars")
                    if exit_code != 0:
                        _logger.warning(f"FAIL {task_id[:12]} | output={output[:300]}")
                    print(f"[task-puller] Task {task_id[:8]} done (exit={exit_code}, {elapsed:.1f}s)")
                except Exception as e:
                    exit_code = 1
                    output = str(e)
                    elapsed = (datetime.now() - start_time).total_seconds()
                    _logger.error(f"ERROR {task_id[:12]} | hand={hand.name} | {elapsed:.1f}s | {e}")
                    print(f"[task-puller] Task {task_id[:8]} failed: {e}")

                # Detect rate-limit signature in the output and persist a
                # cooldown so future tasks bypass this hand. We do this even
                # for hands we fell back TO, not just the originally
                # requested one.
                rl = parse_rate_limit(hand.name, output)
                if rl is not None:
                    hand_registry.mark_rate_limited_from(rl)
                    # Mark the result text with a structured prefix so the
                    # worker (and humans reading the DB) can spot it.
                    output = f"[RATE_LIMITED hand={rl.hand} retry_after_s={rl.retry_after_s} reason={rl.reason}]\n{output}"
                    if exit_code == 0:
                        # Some CLIs exit 0 even when the model errored — bump
                        # to a non-zero so downstream treats it as a failure.
                        exit_code = 429
                    elif exit_code != 429:
                        exit_code = 429

                await _post_result(client, worker_url, auth_key, task_id, exit_code, output)

                # Push generated workspace files back to R2 for cross-node visibility.
                try:
                    from app.workspace_sync import sync_session_now
                    await sync_session_now(session_id or task_id)
                except Exception as e:
                    _logger.warning(f"workspace_sync skipped for {task_id[:12]}: {e}")
        finally:
            _inflight_ids.discard(task_id)


async def _pull_and_execute() -> None:
    """Pull pending tasks and dispatch each to a background executor.

    NEVER awaits task execution — that's the whole point. The poll loop must
    return promptly so it can fire again POLL_INTERVAL seconds later.
    """
    worker_url = os.getenv("CF_WORKER_URL", "")
    node_id = os.getenv("NODE_ID", "")
    auth_key = os.getenv("NODE_TOKEN", "") or os.getenv("CF_WORKER_API_KEY", "")

    if not worker_url or not node_id or not auth_key:
        _logger.warning(f"Skipping poll: worker_url={'set' if worker_url else 'MISSING'} node_id={'set' if node_id else 'MISSING'} auth={'set' if auth_key else 'MISSING'}")
        return

    # Don't even ask for tasks if we're already at capacity. Saves the worker
    # from atomically marking tasks as 'running' that we have nowhere to put.
    available = MAX_CONCURRENT - len(_inflight_ids)
    if available <= 0:
        _logger.debug(f"At capacity ({len(_inflight_ids)}/{MAX_CONCURRENT}) — skipping pull")
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{worker_url}/api/nodes/{node_id}/tasks/pending",
                headers={"X-API-Key": auth_key},
            )
            if resp.status_code != 200:
                _logger.warning(f"Pull failed: HTTP {resp.status_code} — {resp.text[:200]}")
                return

            tasks = resp.json().get("tasks", [])
            if not tasks:
                return

            # Dedupe: don't re-spawn an executor for a task we're already running.
            new_tasks = [t for t in tasks if t["id"] not in _inflight_ids]
            if not new_tasks:
                return

            _logger.info(f"Pulled {len(tasks)} task(s) — {len(new_tasks)} new, spawning executors (cap={MAX_CONCURRENT}, in-flight before={len(_inflight_ids)})")

            for task in new_tasks:
                _inflight_ids.add(task["id"])
                # Fire-and-forget; semaphore inside _execute_one enforces concurrency.
                asyncio.create_task(_execute_one(task, worker_url, auth_key))

    except Exception as e:
        if "ConnectError" not in str(type(e)):
            _logger.error(f"Poll error: {e}")
            print(f"[task-puller] Error: {e}")


async def _sync_workspace_from_r2(client, worker_url, auth_key, session_id, workspace_dir):
    """Download workspace files from R2 to local workspace before execution.
    This enables cross-node context: files generated by other nodes are available locally.
    """
    try:
        resp = await client.get(
            f"{worker_url}/api/sessions/{session_id}/workspace",
            headers={"X-API-Key": auth_key},
            timeout=10,
        )
        if resp.status_code != 200:
            return
        files = resp.json().get("files", [])
        for f in files:
            if f["type"] != "file":
                continue
            local_path = os.path.join(workspace_dir, f["path"])
            if os.path.exists(local_path):
                continue  # Don't overwrite local files
            # Download file content
            try:
                read_resp = await client.get(
                    f"{worker_url}/api/sessions/{session_id}/workspace/read",
                    params={"path": f["path"]},
                    headers={"X-API-Key": auth_key},
                    timeout=15,
                )
                if read_resp.status_code == 200:
                    content = read_resp.json().get("content", "")
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    with open(local_path, "w") as fh:
                        fh.write(content)
                    _logger.info(f"SYNC ← R2: {f['path']} ({len(content)} chars)")
            except Exception:
                pass
    except Exception as e:
        _logger.debug(f"Workspace sync from R2 skipped: {e}")


async def _post_result(client, worker_url, auth_key, task_id, exit_code, result_text):
    """Post task result back to CF Worker callback endpoint."""
    try:
        resp1 = await client.post(
            f"{worker_url}/api/tasks/{task_id}/complete",
            json={"exitCode": exit_code, "resultText": result_text},
            headers={"X-API-Key": auth_key, "Content-Type": "application/json"},
            timeout=30,
        )
        _logger.info(f"CALLBACK {task_id[:12]} | tasks/complete → {resp1.status_code}")
        resp2 = await client.post(
            f"{worker_url}/api/nodes/task-complete/{task_id}",
            json={"exitCode": exit_code, "resultText": result_text},
            headers={"X-API-Key": auth_key, "Content-Type": "application/json"},
            timeout=10,
        )
        _logger.info(f"CALLBACK {task_id[:12]} | nodes/task-complete → {resp2.status_code}")
    except Exception as e:
        _logger.error(f"CALLBACK {task_id[:12]} | FAILED: {e}")
        print(f"[task-puller] Callback failed for {task_id[:8]}: {e}")


async def _poll_loop():
    """Continuously poll for tasks. Auto-restarts on any failure."""
    consecutive_errors = 0
    tick = 0
    while True:
        tick += 1
        try:
            _logger.debug(f"Poll tick #{tick} (in-flight={len(_inflight_ids)}/{MAX_CONCURRENT})")
            await _pull_and_execute()
            consecutive_errors = 0
        except asyncio.CancelledError:
            _logger.info("Poll loop cancelled")
            return
        except Exception as e:
            consecutive_errors += 1
            _logger.error(f"Poll error (#{consecutive_errors}): {type(e).__name__}: {e}")
            print(f"[task-puller] Poll error: {e}")
            # Back off on repeated errors
            if consecutive_errors > 5:
                await asyncio.sleep(30)
        try:
            await asyncio.sleep(POLL_INTERVAL)
        except asyncio.CancelledError:
            _logger.info("Poll loop cancelled during sleep")
            return


def start_task_puller():
    """Start the background task puller with auto-restart."""
    global _puller_task

    async def _resilient_loop():
        """Wrapper that restarts the poll loop if it dies."""
        while True:
            try:
                await _poll_loop()
            except asyncio.CancelledError:
                return
            except Exception as e:
                _logger.error(f"Poll loop died: {e} — restarting in 10s")
                await asyncio.sleep(10)

    _puller_task = asyncio.create_task(_resilient_loop())
    _logger.info(f"Task puller started (interval={POLL_INTERVAL}s, max_concurrent={MAX_CONCURRENT}, node={os.getenv('NODE_ID','?')}, worker={os.getenv('CF_WORKER_URL','?')})")
    print(f"[task-puller] Started (polling every {POLL_INTERVAL}s, max {MAX_CONCURRENT} concurrent)")


def stop_task_puller():
    """Stop the task puller."""
    global _puller_task
    if _puller_task:
        _puller_task.cancel()
        _puller_task = None


def _cooldown_summary(registry, names: list[str]) -> str:
    """One-line cooldown status for each name, used in 429 result text."""
    lines = []
    for n in names:
        info = registry.cooldown_info(n)
        if info is None:
            lines.append(f"  {n}: available")
        else:
            from datetime import datetime as _dt
            until = _dt.fromtimestamp(info["until"]).isoformat(timespec="seconds")
            lines.append(f"  {n}: cool until {until} (reason={info.get('reason','?')})")
    return "\n".join(lines)
