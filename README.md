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

### 1. Register with a worker

```bash
agent-route-node register \
  --worker-url https://agent-route.example.workers.dev \
  --admin-key sk_admin_xxx \
  --name "My Mac Studio"
```

This will:
- Test your available CLI agents (gemini, claude, codex)
- Register your node with the cloud orchestrator
- Generate a node token (`nk_...`) for authentication
- Save config to `~/.agent-route/.env`

### 2. Start the node

```bash
agent-route-node start
```

### 3. Check status

```bash
agent-route-node status
```

## What it does

Your edge node:
- Runs AI CLI agents locally via subprocess (gemini, claude, codex, ollama)
- Registers with the Agent Route cloud worker as an available node
- Receives tasks from the load balancer based on capacity and performance
- Streams execution output in real-time via WebSocket
- Syncs workspace files to cloud storage (R2)
- Sends heartbeats to stay in the fleet

## Requirements

- Python 3.11+
- At least one CLI agent installed:
  - `npx @anthropic-ai/claude-code` (Claude Code)
  - `npx gemini` (Gemini CLI)
  - `npx codex` (Codex CLI)
  - Ollama running locally (optional)

## Configuration

All config lives in `~/.agent-route/.env` (or `AGENT_ROUTE_HOME`):

```env
# Set by registration
CF_WORKER_URL=https://agent-route.example.workers.dev
NODE_TOKEN=nk_...
NODE_ID=...
NODE_URL=http://localhost:8017

# Agent toggles
ENABLE_GEMINI_CLI=true
ENABLE_CLAUDE_REMOTE_CONTROL=true
ENABLE_CODEX_SERVER=true
ENABLE_OLLAMA_API=true
```

## Using your token

The node token (`nk_...`) works as SSO across:
- **Agent Route Frontend** — login at the web dashboard
- **Infinite Research** — authenticate research sessions
- **API access** — `curl -H "X-API-Key: nk_..." https://worker/api/...`
