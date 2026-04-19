#!/usr/bin/env python3
"""
Test each hand locally — diagnose why CLI tools fail on this node.
All output logged to ~/.agent-route/test_hands.log

Usage:
    python test_hands.py
    python test_hands.py gemini
    python test_hands.py claude codex
"""
import asyncio
import os
import sys
import shutil
import time
import logging
from datetime import datetime

# Load env
from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(__file__))
from app.config import get_env_path, get_workspaces_dir, get_data_dir
load_dotenv(dotenv_path=get_env_path())

# Set up dual logging — console + file
LOG_FILE = os.path.join(get_data_dir(), "test_hands.log")
logger = logging.getLogger("test_hands")
logger.setLevel(logging.DEBUG)
# File handler — full detail
fh = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(fh)
# Console handler — concise
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(ch)


def log(msg, level="info"):
    getattr(logger, level)(msg)


def check_env():
    """Check critical environment variables."""
    log("=" * 60)
    log(f"Test started: {datetime.now().isoformat()}")
    log(f"Log file: {LOG_FILE}")
    log(f"Env file: {get_env_path()}")
    log(f"Data dir: {get_data_dir()}")
    log(f"Workspaces: {get_workspaces_dir()}")
    log("=" * 60)
    log("")
    log("=== Environment Variables ===")

    keys = [
        "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
        "CF_WORKER_URL", "NODE_ID", "NODE_TOKEN", "NODE_KEY", "NODE_NAME",
        "ENABLE_GEMINI_CLI", "ENABLE_CLAUDE_REMOTE_CONTROL", "ENABLE_CODEX_SERVER",
        "ENABLE_OLLAMA_API", "ENABLE_MFLUX_IMAGE", "NODE_BIN_DIR",
        "PATH",
    ]
    for k in keys:
        v = os.getenv(k, "")
        if k == "PATH":
            dirs = v.split(os.pathsep)
            log(f"  {k}: ({len(dirs)} dirs)")
            for d in dirs:
                exists = os.path.isdir(d)
                log(f"    {'OK' if exists else 'MISSING':7} {d}", "debug")
        elif "KEY" in k and v and len(v) > 12:
            log(f"  {k:35} = {v[:8]}...{v[-4:]}")
        elif v:
            log(f"  {k:35} = {v}")
        else:
            log(f"  {k:35} = (not set)", "warning")
    log("")


def check_binaries():
    """Check if CLI binaries are findable."""
    log("=== CLI Binary Resolution ===")
    from app.hands.base import resolve_cli_path, get_cli_env

    env = get_cli_env()
    env_path = env.get("PATH", "")
    log(f"  Subprocess PATH dirs: {len(env_path.split(os.pathsep))}")
    for d in env_path.split(os.pathsep):
        exists = os.path.isdir(d)
        log(f"    {'OK' if exists else 'MISSING':7} {d}", "debug")

    log("")
    for binary in ["node", "npx", "gemini", "claude", "codex", "git", "ollama"]:
        resolved = resolve_cli_path(binary)
        which_result = shutil.which(binary)
        exists = os.path.isfile(resolved) if resolved != binary else False
        status = "OK" if (exists or which_result) else "NOT FOUND"
        log(f"  {binary:10} → {resolved:50} [{status}]")
        if which_result and which_result != resolved:
            log(f"  {' ':10}   which() found: {which_result}", "debug")
        if status == "NOT FOUND":
            log(f"  {' ':10}   *** Install this binary or set NODE_BIN_DIR ***", "warning")

    log("")
    log("  Subprocess API keys:")
    for k in ["ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"]:
        v = env.get(k, "")
        if v:
            log(f"    {k} = {v[:8]}...{v[-4:]}")
        else:
            log(f"    {k} = MISSING — CLI auth will fail!", "warning")
    log("")


async def test_hand(hand_name: str):
    """Test a specific hand with a simple prompt."""
    from app.hands.registry import hand_registry, auto_register_all

    if len(hand_registry) == 0:
        auto_register_all()

    hand = hand_registry.get(hand_name)
    if not hand:
        log(f"  [{hand_name}] Hand not registered — skipping", "warning")
        return False

    log(f"  [{hand_name}] Starting test...")
    log(f"  [{hand_name}] Hand type: {hand.hand_type}", "debug")
    log(f"  [{hand_name}] Description: {hand.description}", "debug")

    workspace = os.path.join(get_workspaces_dir(), f"_test_{hand_name}")
    os.makedirs(workspace, exist_ok=True)
    log(f"  [{hand_name}] Workspace: {workspace}", "debug")

    prompt = "Reply with exactly: HAND_TEST_OK"
    log(f"  [{hand_name}] Prompt: {prompt}", "debug")

    start = time.time()
    try:
        result = await asyncio.wait_for(
            hand.execute(prompt, workspace_dir=workspace),
            timeout=120,
        )
        elapsed = time.time() - start
        ok = result.exit_code == 0
        output = result.output.strip()

        log(f"  [{hand_name}] Exit code: {result.exit_code}")
        log(f"  [{hand_name}] Elapsed: {elapsed:.1f}s")
        log(f"  [{hand_name}] Output length: {len(output)} chars")
        log(f"  [{hand_name}] Output (full):", "debug")
        for line in output.split('\n')[:50]:
            log(f"    | {line}", "debug")
        if len(output.split('\n')) > 50:
            log(f"    | ... ({len(output.split(chr(10)))} total lines)", "debug")

        if ok:
            log(f"  [{hand_name}] PASS")
        else:
            log(f"  [{hand_name}] FAIL (exit={result.exit_code})", "warning")
            # Diagnose common errors
            out_lower = output.lower()
            if "authentication" in out_lower or "401" in out_lower or "api key" in out_lower:
                log(f"  [{hand_name}] → DIAGNOSIS: API key missing or invalid", "error")
                log(f"  [{hand_name}] → FIX: Add the API key to ~/.agent-route/.env or shell profile", "error")
            elif "not found" in out_lower or "command not found" in out_lower or "enoent" in out_lower:
                log(f"  [{hand_name}] → DIAGNOSIS: CLI binary not found in PATH", "error")
                log(f"  [{hand_name}] → FIX: Install the CLI tool or set NODE_BIN_DIR", "error")
            elif "timeout" in out_lower:
                log(f"  [{hand_name}] → DIAGNOSIS: Execution timed out", "error")
            elif "permission" in out_lower:
                log(f"  [{hand_name}] → DIAGNOSIS: Permission denied", "error")
            elif "rate limit" in out_lower or "429" in out_lower:
                log(f"  [{hand_name}] → DIAGNOSIS: Rate limited by API provider", "warning")
        return ok

    except asyncio.TimeoutError:
        elapsed = time.time() - start
        log(f"  [{hand_name}] TIMEOUT after {elapsed:.1f}s (limit=120s)", "error")
        return False
    except Exception as e:
        elapsed = time.time() - start
        log(f"  [{hand_name}] EXCEPTION after {elapsed:.1f}s: {e}", "error")
        import traceback
        log(f"  [{hand_name}] Traceback:", "debug")
        for line in traceback.format_exc().split('\n'):
            log(f"    {line}", "debug")
        return False


async def test_task_puller():
    """Test if the task puller can reach the CF Worker."""
    import httpx

    worker_url = os.getenv("CF_WORKER_URL", "")
    node_id = os.getenv("NODE_ID", "")
    auth_key = os.getenv("NODE_TOKEN", "") or os.getenv("CF_WORKER_API_KEY", "")

    log("=== Task Puller Connectivity ===")
    if not worker_url:
        log("  CF_WORKER_URL not set — task puller disabled", "warning")
        return
    if not node_id:
        log("  NODE_ID not set — not registered", "warning")
        return

    log(f"  Worker URL: {worker_url}")
    log(f"  Node ID: {node_id}")
    log(f"  Auth key: {auth_key[:12]}...", "debug")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Test pull endpoint
            log(f"  Request: GET {worker_url}/api/nodes/{node_id}/tasks/pending", "debug")
            resp = await client.get(
                f"{worker_url}/api/nodes/{node_id}/tasks/pending",
                headers={"X-API-Key": auth_key},
            )
            log(f"  Response: {resp.status_code}")
            log(f"  Body: {resp.text[:300]}", "debug")

            if resp.status_code == 200:
                tasks = resp.json().get("tasks", [])
                log(f"  Pending tasks: {len(tasks)}")
                for t in tasks:
                    log(f"    Task {t['id'][:8]} | {t['hand_name']} | {t['prompt'][:60]}...", "debug")
            elif resp.status_code == 401:
                log("  → AUTH FAILED: Token invalid or expired", "error")
            else:
                log(f"  → Error: {resp.text[:200]}", "error")

            # Test heartbeat
            log(f"  Request: POST {worker_url}/api/nodes/{node_id}/heartbeat", "debug")
            resp2 = await client.post(
                f"{worker_url}/api/nodes/{node_id}/heartbeat",
                json={},
                headers={"X-API-Key": auth_key, "Content-Type": "application/json"},
            )
            log(f"  Heartbeat: {resp2.status_code}")

    except Exception as e:
        log(f"  Connection failed: {e}", "error")
    log("")


async def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["gemini", "claude", "codex", "ollama"]

    check_env()
    check_binaries()
    await test_task_puller()

    log("=== Hand Execution Tests ===")
    results = {}
    for hand in targets:
        ok = await test_hand(hand)
        results[hand] = ok
        log("")

    log("=" * 60)
    log("=== Summary ===")
    for hand, ok in results.items():
        log(f"  {hand:10} {'PASS' if ok else 'FAIL'}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    log(f"\n  {passed}/{total} hands passed")

    if passed < total:
        log("")
        log("  Troubleshooting:")
        log("  - AUTH errors → Add API keys to ~/.agent-route/.env")
        log("  - NOT FOUND  → Install CLI tools or set NODE_BIN_DIR=/path/to/bin")
        log("  - TIMEOUT    → Check network connectivity")
        log(f"\n  Full log: {LOG_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
