#!/usr/bin/env python3
"""
Debug a single hand — shows exact command, env, and raw output.

Usage:
    python debug_hand.py gemini
    python debug_hand.py claude
    python debug_hand.py codex
"""
import asyncio
import os
import sys
import shutil
import subprocess

from dotenv import load_dotenv
sys.path.insert(0, os.path.dirname(__file__))
from app.config import get_env_path
load_dotenv(dotenv_path=get_env_path())
from app.hands.base import resolve_cli_path, get_cli_env


async def main():
    hand = sys.argv[1] if len(sys.argv) > 1 else "gemini"
    prompt = "Reply with exactly: TEST_OK"

    env = get_cli_env()
    print(f"=== Debug: {hand} ===\n")

    # Show key env vars
    print("HOME:", env.get("HOME"))
    print("USER:", env.get("USER"))
    print("SHELL:", env.get("SHELL"))
    print("XDG_CONFIG_HOME:", env.get("XDG_CONFIG_HOME"))
    print()

    # Check what binaries resolve to
    npx_path = resolve_cli_path("npx")
    direct_path = resolve_cli_path(hand)
    print(f"npx resolves to:      {npx_path}")
    print(f"'{hand}' resolves to: {direct_path}")
    print(f"shutil.which('npx'):  {shutil.which('npx')}")
    print(f"shutil.which('{hand}'): {shutil.which(hand)}")
    print()

    # Check auth config directories
    auth_dirs = {
        "claude": ["~/.claude", "~/.config/claude"],
        "gemini": ["~/.config/gemini", "~/.gemini"],
        "codex": ["~/.codex", "~/.config/codex"],
    }
    print("Auth config dirs:")
    for d in auth_dirs.get(hand, []):
        expanded = os.path.expanduser(d)
        exists = os.path.isdir(expanded)
        files = os.listdir(expanded) if exists else []
        print(f"  {d:30} {'EXISTS' if exists else 'MISSING':8} {files[:5]}")
    print()

    # Build exact command like the hand does
    if hand == "gemini":
        cmd = [npx_path, "gemini", "-p", prompt, "--output-format", "json", "--yolo"]
        cmd_direct = [direct_path, "-p", prompt] if direct_path != hand else None
    elif hand == "claude":
        cmd = [npx_path, "@anthropic-ai/claude-code", "-p", "--dangerously-skip-permissions", prompt]
        cmd_direct = [direct_path, "-p", "--dangerously-skip-permissions", prompt] if direct_path != hand else None
    elif hand == "codex":
        cmd = [npx_path, "codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", prompt]
        cmd_direct = [direct_path, "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", prompt] if direct_path != hand else None
    else:
        cmd = [direct_path, prompt]
        cmd_direct = None

    # Test 1: via npx (what the hand does)
    print(f"--- Test 1: via npx ---")
    print(f"Command: {' '.join(cmd[:4])}...")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        print(f"Exit code: {proc.returncode}")
        print(f"Stdout ({len(stdout)} bytes):")
        for line in stdout.decode(errors='replace').split('\n')[:10]:
            print(f"  | {line}")
        print(f"Stderr ({len(stderr)} bytes):")
        for line in stderr.decode(errors='replace').split('\n')[:10]:
            print(f"  | {line}")
    except asyncio.TimeoutError:
        print("  TIMEOUT (>30s)")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

    # Test 2: direct binary (what you run in terminal)
    if cmd_direct:
        print(f"--- Test 2: direct binary ---")
        print(f"Command: {' '.join(cmd_direct[:4])}...")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_direct,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/tmp",
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            print(f"Exit code: {proc.returncode}")
            print(f"Stdout ({len(stdout)} bytes):")
            for line in stdout.decode(errors='replace').split('\n')[:10]:
                print(f"  | {line}")
            print(f"Stderr ({len(stderr)} bytes):")
            for line in stderr.decode(errors='replace').split('\n')[:10]:
                print(f"  | {line}")
        except asyncio.TimeoutError:
            print("  TIMEOUT (>30s)")
        except Exception as e:
            print(f"  ERROR: {e}")
        print()

    # Test 3: pure shell (closest to what you do in terminal)
    print(f"--- Test 3: via shell (like you type it) ---")
    shell = os.getenv("SHELL", "/bin/bash")
    shell_cmd = f'{hand} -p "{prompt}" 2>&1 | head -5'
    if hand == "claude":
        shell_cmd = f'claude -p --dangerously-skip-permissions "{prompt}" 2>&1 | head -5'
    print(f"Shell: {shell} -l -c '{shell_cmd}'")
    try:
        proc = subprocess.run(
            [shell, "-l", "-c", shell_cmd],
            capture_output=True, text=True, timeout=30,
        )
        print(f"Exit code: {proc.returncode}")
        print(f"Output:")
        for line in proc.stdout.split('\n')[:10]:
            print(f"  | {line}")
        if proc.stderr:
            print(f"Stderr:")
            for line in proc.stderr.split('\n')[:5]:
                print(f"  | {line}")
    except subprocess.TimeoutExpired:
        print("  TIMEOUT (>30s)")
    except Exception as e:
        print(f"  ERROR: {e}")


if __name__ == "__main__":
    asyncio.run(main())
