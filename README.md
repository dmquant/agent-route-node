# Agent Route Edge Node

Edge node for the [Agent Route](https://github.com/dmquant/agent-route) multi-agent orchestration network. Run AI agents (Gemini CLI, Claude Code, Codex, Ollama) on your local machine and connect them to the cloud orchestrator.

## Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/dmquant/agent-route-node/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/dmquant/agent-route-node.git
cd agent-route-node
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

### 1. Get an invite key

Ask your network admin to generate an invite key (`ik_...`) from the **Edge Nodes** page in the Agent Route dashboard. Each key is single-use and may have an expiration.

### 2. Register your node

```bash
agent-route-node register \
  --worker-url https://agent-route.example.workers.dev \
  --invite-key ik_your_invite_key_here \
  --name "My Mac Studio"
```

This will:
- Test your available CLI agents (gemini, claude, codex)
- Register your node with the cloud orchestrator
- Generate a unique node token (`nk_...`) for ongoing authentication
- Save all config to `~/.agent-route/.env`

No admin key is needed — the invite key handles authorization.

### 3. Start the node

```bash
agent-route-node start
```

### 4. Check status

```bash
agent-route-node status
```

### 5. Stop the node

```bash
./stop.sh
```

### 6. Update to latest version

```bash
cd ~/.agent-route/node
git pull
./stop.sh
./start.sh
```

Or one-liner:

```bash
cd ~/.agent-route/node && git pull && ./stop.sh && ./start.sh
```

## What it does

Your edge node:
- Runs AI CLI agents locally via subprocess (gemini, claude, codex, ollama)
- Registers with the Agent Route cloud worker as an available node
- Receives tasks from the load balancer based on capacity and performance
- Streams execution output in real-time via WebSocket
- Syncs workspace files to cloud storage (R2)
- Sends heartbeats every 30s to stay in the fleet

## Requirements

- Python 3.11+
- At least one CLI agent installed:
  - `npx @anthropic-ai/claude-code` (Claude Code)
  - `npx gemini` (Gemini CLI)
  - `npx codex` (Codex CLI)
  - Ollama running locally (optional)

## Configuration

All config lives in `~/.agent-route/.env` (or set `AGENT_ROUTE_HOME`):

```env
# Set automatically by registration
CF_WORKER_URL=https://agent-route.example.workers.dev
NODE_TOKEN=nk_...
NODE_ID=...
NODE_URL=http://localhost:8017
NODE_KEY=dcpn_...

# Agent toggles
ENABLE_GEMINI_CLI=true
ENABLE_CLAUDE_REMOTE_CONTROL=true
ENABLE_CODEX_SERVER=true
ENABLE_OLLAMA_API=true
```

## Using your token

After registration, you receive a node token (`nk_...`). This works as SSO across:

- **Agent Route Frontend** — enter the token to login at the web dashboard
- **Infinite Research** — same token authenticates research sessions
- **API access** — `curl -H "X-API-Key: nk_..." https://worker/api/...`

## For Admins

To generate invite keys for new nodes:

1. Login to the Agent Route dashboard with your admin key (`sk_admin_...`)
2. Go to **Edge Nodes** page
3. Expand **Invite Keys** section
4. Click **Generate** — optionally add a label
5. Copy the `ik_...` key and share it with the node operator

Each invite key is single-use and consumed upon successful registration.
