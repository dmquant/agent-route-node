#!/usr/bin/env python3
"""
Standalone CLI to register an edge node with the agent-route CF Worker.

Usage:
    python register_node.py --worker-url https://agent-route.dmquant.workers.dev \
                            --admin-key sk_admin_xxx \
                            --name "My Mac Studio" \
                            --node-url http://localhost:8017
"""
import argparse
import asyncio
import json
import os
import sys

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(__file__))


async def main():
    parser = argparse.ArgumentParser(description="Register edge node with agent-route")
    parser.add_argument("--worker-url", required=True, help="CF Worker URL")
    parser.add_argument("--admin-key", required=True, help="Admin API key or existing node token")
    parser.add_argument("--name", default=None, help="Node display name")
    parser.add_argument("--node-url", default="http://localhost:8017", help="This node's reachable URL")
    parser.add_argument("--node-key", default=None, help="Node API key for task auth")
    args = parser.parse_args()

    import httpx

    # Discover available hands by probing the local bridge
    hands = []
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{args.node_url}/api/hands")
            if resp.status_code == 200:
                data = resp.json()
                hands = [{"name": h["name"]} for h in data.get("hands", [])]
                print(f"Discovered hands: {[h['name'] for h in hands]}")
    except Exception as e:
        print(f"Could not probe local bridge at {args.node_url}: {e}")
        print("Make sure the API bridge is running (agent-route-node start)")
        sys.exit(1)

    if not hands:
        print("No hands found. Aborting.")
        sys.exit(1)

    import platform
    body = {
        "name": args.name or f"{platform.node()} API Bridge",
        "apiUrl": args.node_url,
        "apiKey": args.node_key or args.admin_key,
        "hands": hands,
        "platform": {
            "os": platform.system().lower(),
            "arch": platform.machine(),
            "python": platform.python_version(),
        },
    }

    print(f"\nRegistering with {args.worker_url}...")
    print(f"  Name: {body['name']}")
    print(f"  URL: {body['apiUrl']}")
    print(f"  Hands: {[h['name'] for h in hands]}")

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{args.worker_url}/api/nodes/register",
            json=body,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": args.admin_key,
            },
        )

    if resp.status_code >= 400:
        print(f"\nRegistration FAILED ({resp.status_code}):")
        try:
            data = resp.json()
            print(f"  Error: {data.get('error', 'Unknown')}")
            for fh in data.get("testResults", []):
                print(f"  Hand '{fh['hand']}' failed: {fh.get('error', '?')}")
        except Exception:
            print(f"  {resp.text}")
        sys.exit(1)

    data = resp.json()
    print(f"\nRegistration SUCCESSFUL!")
    print(f"  Node ID: {data['nodeId']}")
    print(f"  Token:   {data['token']}")
    print(f"  Verified: {data.get('verifiedHands', [])}")
    if data.get("failedHands"):
        for fh in data["failedHands"]:
            print(f"  Failed:  {fh['hand']} — {fh.get('error', '?')}")

    # Write to .env
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    lines = []
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            lines = f.readlines()

    updates = {"NODE_TOKEN": data["token"], "NODE_ID": data["nodeId"]}
    for key, val in updates.items():
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={val}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={val}\n")

    with open(env_path, 'w') as f:
        f.writelines(lines)

    print(f"\nToken written to {os.path.abspath(env_path)}")
    print(f"\nUse this token for:")
    print(f"  - Frontend login: enter '{data['token'][:20]}...'")
    print(f"  - Infinite Research: set VITE_API_KEY={data['token'][:20]}...")
    print(f"  - API access: curl -H 'X-API-Key: {data['token'][:20]}...' ...")


if __name__ == "__main__":
    asyncio.run(main())
