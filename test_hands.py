#!/usr/bin/env python3
"""
Test each hand locally — diagnose why CLI tools fail on this node.

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

# Load env
from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(__file__))
from app.config import get_env_path, get_workspaces_dir
load_dotenv(dotenv_path=get_env_path())


def check_env():
    """Check critical environment variables."""
    print("=== Environment ===")
    keys = [
        "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY",
        "CF_WORKER_URL", "NODE_ID", "NODE_TOKEN",
        "ENABLE_GEMINI_CLI", "ENABLE_CLAUDE_REMOTE_CONTROL", "ENABLE_CODEX_SERVER",
        "ENABLE_OLLAMA_API", "ENABLE_MFLUX_IMAGE",
    ]
    for k in keys:
        v = os.getenv(k, "")
        if "KEY" in k and v:
            print(f"  {k:35} = {v[:8]}...{v[-4:]}")
        elif v:
            print(f"  {k:35} = {v}")
        else:
            print(f"  {k:35} = (not set)")
    print()


def check_binaries():
    """Check if CLI binaries are findable."""
    print("=== CLI Binaries ===")
    from app.hands.base import resolve_cli_path, get_cli_env

    env = get_cli_env()
    path = env.get("PATH", "")
    print(f"  PATH dirs: {len(path.split(os.pathsep))}")

    for binary in ["node", "npx", "gemini", "claude", "codex", "git", "ollama"]:
        resolved = resolve_cli_path(binary)
        found = shutil.which(binary) or resolve_cli_path(binary)
        exists = os.path.isfile(resolved) if resolved != binary else False
        status = "OK" if (exists or shutil.which(binary)) else "NOT FOUND"
        print(f"  {binary:10} → {resolved:50} [{status}]")

    # Check API keys in subprocess env
    print()
    print("  Subprocess env API keys:")
    for k in ["ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY"]:
        v = env.get(k, "")
        if v:
            print(f"    {k} = {v[:8]}...{v[-4:]}")
        else:
            print(f"    {k} = (MISSING — CLI will fail auth)")
    print()


async def test_hand(hand_name: str):
    """Test a specific hand with a simple prompt."""
    from app.hands.registry import hand_registry, auto_register_all

    if len(hand_registry) == 0:
        auto_register_all()

    hand = hand_registry.get(hand_name)
    if not hand:
        print(f"  [{hand_name}] Hand not registered")
        return False

    print(f"  [{hand_name}] Testing...")
    workspace = os.path.join(get_workspaces_dir(), f"_test_{hand_name}")
    os.makedirs(workspace, exist_ok=True)

    start = time.time()
    try:
        result = await asyncio.wait_for(
            hand.execute("Reply with exactly: HAND_TEST_OK", workspace_dir=workspace),
            timeout=60,
        )
        elapsed = time.time() - start
        ok = result.exit_code == 0
        output = result.output.strip()[:200]
        print(f"  [{hand_name}] {'PASS' if ok else 'FAIL'} (exit={result.exit_code}, {elapsed:.1f}s)")
        print(f"  [{hand_name}] Output: {output}")
        if not ok and result.output:
            # Check for common errors
            out = result.output.lower()
            if "authentication" in out or "401" in out or "api key" in out:
                print(f"  [{hand_name}] → AUTH ERROR: API key missing or invalid in subprocess env")
            elif "not found" in out or "command not found" in out:
                print(f"  [{hand_name}] → BINARY NOT FOUND: CLI tool not in PATH")
            elif "timeout" in out:
                print(f"  [{hand_name}] → TIMEOUT: Tool took too long")
        return ok
    except asyncio.TimeoutError:
        print(f"  [{hand_name}] TIMEOUT (>60s)")
        return False
    except Exception as e:
        print(f"  [{hand_name}] ERROR: {e}")
        return False


async def test_task_puller():
    """Test if the task puller can reach the CF Worker."""
    import httpx

    worker_url = os.getenv("CF_WORKER_URL", "")
    node_id = os.getenv("NODE_ID", "")
    auth_key = os.getenv("NODE_TOKEN", "") or os.getenv("CF_WORKER_API_KEY", "")

    print("=== Task Puller Connectivity ===")
    if not worker_url:
        print("  CF_WORKER_URL not set — task puller disabled")
        return
    if not node_id:
        print("  NODE_ID not set — not registered")
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{worker_url}/api/nodes/{node_id}/tasks/pending",
                headers={"X-API-Key": auth_key},
            )
            print(f"  Pull endpoint: {resp.status_code}")
            if resp.status_code == 200:
                tasks = resp.json().get("tasks", [])
                print(f"  Pending tasks: {len(tasks)}")
            else:
                print(f"  Error: {resp.text[:100]}")
    except Exception as e:
        print(f"  Connection failed: {e}")
    print()


async def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["gemini", "claude", "codex", "ollama"]

    check_env()
    check_binaries()
    await test_task_puller()

    print("=== Hand Tests ===")
    results = {}
    for hand in targets:
        ok = await test_hand(hand)
        results[hand] = ok
        print()

    print("=== Summary ===")
    for hand, ok in results.items():
        print(f"  {hand:10} {'PASS' if ok else 'FAIL'}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  {passed}/{total} hands passed")

    if passed < total:
        print("\n  Troubleshooting:")
        print("  - AUTH errors: Add API keys to ~/.agent-route/.env or shell profile")
        print("  - NOT FOUND: Install CLI tools (npx, gemini, claude, codex)")
        print("  - Set NODE_BIN_DIR=/path/to/node/bin in .env if node is in custom location")


if __name__ == "__main__":
    asyncio.run(main())
