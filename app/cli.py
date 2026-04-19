#!/usr/bin/env python3
"""
CLI entry point for agent-route-node.

Usage:
    agent-route-node register --worker-url URL --admin-key KEY [--name NAME]
    agent-route-node start [--port PORT]
    agent-route-node status
"""
import argparse
import asyncio
import os
import sys


def cmd_register(args):
    """Register this node with an agent-route worker."""
    # Import here to avoid loading everything on --help
    from app.config import get_env_path
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=get_env_path())

    import httpx
    import platform

    worker_url = args.worker_url or os.getenv("CF_WORKER_URL", "")
    invite_key = args.invite_key or os.getenv("INVITE_KEY", "")
    admin_key = args.admin_key or os.getenv("CF_WORKER_API_KEY", "")
    node_url = args.node_url or os.getenv("NODE_URL", f"http://localhost:{args.port}")
    node_name = args.name or os.getenv("NODE_NAME", f"{platform.node()}")

    if not worker_url:
        print("Error: --worker-url is required (or set CF_WORKER_URL)")
        sys.exit(1)
    if not invite_key and not admin_key:
        print("Error: --invite-key or --admin-key is required")
        print("  Get an invite key from your admin, or use admin key for self-registration")
        sys.exit(1)

    # Discover hands by probing local bridge (if running)
    hands = []
    try:
        resp = httpx.get(f"http://localhost:{args.port}/api/hands", timeout=3)
        if resp.status_code == 200:
            hands = [{"name": h["name"]} for h in resp.json().get("hands", [])]
    except Exception:
        # Bridge not running — register with default hands
        for cmd, name in [("gemini", "gemini"), ("claude", "claude"), ("codex", "codex")]:
            import shutil
            if shutil.which(cmd) or shutil.which("npx"):
                hands.append({"name": name})

    if not hands:
        hands = [{"name": "gemini"}, {"name": "claude"}, {"name": "codex"}]
        print(f"Could not probe local bridge — registering with default hands: {[h['name'] for h in hands]}")

    print(f"\nRegistering with {worker_url}")
    print(f"  Name: {node_name}")
    print(f"  URL:  {node_url}")
    print(f"  Hands: {[h['name'] for h in hands]}")

    node_secret = os.urandom(16).hex()
    body = {
        "name": node_name,
        "apiUrl": node_url,
        "apiKey": f"dcpn_{node_secret}",
        "hands": hands,
        "platform": {"os": platform.system().lower(), "arch": platform.machine(), "python": platform.python_version()},
    }
    if invite_key:
        body["inviteKey"] = invite_key

    headers_dict = {"Content-Type": "application/json"}
    if admin_key:
        headers_dict["X-API-Key"] = admin_key

    resp = httpx.post(
        f"{worker_url}/api/nodes/register", json=body,
        headers=headers_dict,
        timeout=120,
    )

    if resp.status_code >= 400:
        print(f"\nRegistration FAILED ({resp.status_code}):")
        try:
            data = resp.json()
            print(f"  {data.get('error', resp.text)}")
            for fh in data.get("testResults", []):
                print(f"  Hand '{fh['hand']}' failed: {fh.get('error', '?')}")
        except Exception:
            print(f"  {resp.text}")
        sys.exit(1)

    data = resp.json()
    print(f"\nRegistered successfully!")
    print(f"  Node ID: {data['nodeId']}")
    print(f"  Token:   {data['token']}")
    if data.get("verifiedHands"):
        print(f"  Verified: {data['verifiedHands']}")
    if data.get("failedHands"):
        for fh in data["failedHands"]:
            print(f"  Failed:  {fh['hand']} — {fh.get('error', '?')}")

    # Write full .env
    env_path = get_env_path()

    # If file doesn't exist or is mostly empty, write a complete template
    existing_lines = 0
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            existing_lines = sum(1 for l in f if l.strip() and not l.startswith('#'))

    if existing_lines < 5:
        # Write full config
        with open(env_path, 'w') as f:
            f.write(f"""# ─── Agent Enable Flags ───
ENABLE_GEMINI_CLI=true
ENABLE_CLAUDE_REMOTE_CONTROL=true
ENABLE_CODEX_SERVER=true
ENABLE_OLLAMA_API=false
ENABLE_MFLUX_IMAGE=false
ENABLE_VANE_SEARCH=false

# ─── Node Identity (set by registration) ───
CF_WORKER_URL={worker_url}
NODE_ID={data["nodeId"]}
NODE_NAME="{node_name}"
NODE_URL={node_url}
NODE_KEY=dcpn_{node_secret}
NODE_TOKEN={data["token"]}

# ─── Ollama (optional — set ENABLE_OLLAMA_API=true to use) ───
# OLLAMA_HOST=http://localhost:11434
# OLLAMA_MODEL=llama3.2

# ─── Vane AI Search (optional — set ENABLE_VANE_SEARCH=true to use) ───
# VANE_URL=http://localhost:3000
# VANE_CHAT_MODEL=gemma4:26b
# VANE_EMBED_MODEL=nomic-embed-text:latest
""")
    else:
        # Update existing .env with registration data
        _write_env(env_path, {
            "CF_WORKER_URL": worker_url,
            "NODE_ID": data["nodeId"],
            "NODE_NAME": f'"{node_name}"',
            "NODE_URL": node_url,
            "NODE_KEY": f"dcpn_{node_secret}",
            "NODE_TOKEN": data["token"],
        })

    print(f"\nConfig written to {env_path}")
    print(f"\nUse this token for frontend login:")
    print(f"  {data['token']}")
    print(f"\nEdit {env_path} to enable/disable hands")
    print(f"\nNext: agent-route-node start")


def cmd_start(args):
    """Start the edge node API bridge."""
    from app.config import get_env_path
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=get_env_path())

    port = args.port or int(os.getenv("NODE_URL", "http://localhost:8017").rsplit(":", 1)[-1])

    import uvicorn
    print(f"[agent-route-node] Starting on port {port}...")
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=args.reload)


def cmd_status(args):
    """Check node status."""
    from app.config import get_env_path
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=get_env_path())

    import httpx
    port = int(os.getenv("NODE_URL", "http://localhost:8017").rsplit(":", 1)[-1])

    print("Local bridge:")
    try:
        resp = httpx.get(f"http://localhost:{port}/api/hands", timeout=3)
        hands = resp.json().get("hands", [])
        print(f"  Status: online (port {port})")
        print(f"  Hands:  {[h['name'] for h in hands]}")
    except Exception:
        print(f"  Status: offline (port {port})")

    worker_url = os.getenv("CF_WORKER_URL", "")
    token = os.getenv("NODE_TOKEN", "")
    if worker_url and token:
        print(f"\nCF Worker ({worker_url}):")
        try:
            resp = httpx.get(f"{worker_url}/api/node/info", headers={"X-API-Key": token}, timeout=5)
            info = resp.json()
            print(f"  Connected: yes")
            print(f"  Hands:     {[h['name'] for h in info.get('hands', [])]}")
            print(f"  Nodes:     {info.get('edgeNodeCount', 0)}")
        except Exception as e:
            print(f"  Connected: no ({e})")
    else:
        print("\nCF Worker: not configured")


def _write_env(path: str, updates: dict):
    """Upsert key=value pairs into a .env file."""
    lines = []
    if os.path.exists(path):
        with open(path, 'r') as f:
            lines = f.readlines()

    for key, val in updates.items():
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={val}\n")

    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w') as f:
        f.writelines(lines)


def main():
    parser = argparse.ArgumentParser(
        prog="agent-route-node",
        description="Edge node for the Agent Route multi-agent orchestration network",
    )
    sub = parser.add_subparsers(dest="command")

    # register
    reg = sub.add_parser("register", help="Register with an agent-route worker")
    reg.add_argument("--worker-url", help="CF Worker URL (e.g. https://agent-route.example.workers.dev)")
    reg.add_argument("--invite-key", help="Invite key from admin (ik_...)")
    reg.add_argument("--admin-key", help="Admin API key (alternative to invite key)")
    reg.add_argument("--name", help="Display name for this node")
    reg.add_argument("--node-url", help="This node's reachable URL (default: http://localhost:PORT)")
    reg.add_argument("--port", type=int, default=8017, help="Port (default: 8017)")

    # start
    st = sub.add_parser("start", help="Start the edge node service")
    st.add_argument("--port", type=int, help="Port (default: from .env or 8017)")
    st.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    # status
    sub.add_parser("status", help="Check node and connection status")

    args = parser.parse_args()
    if args.command == "register":
        cmd_register(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
