# Agent Route Edge Node

Edge node for the [Agent Route](https://github.com/dmquant/agent-route) multi-agent orchestration network. Run AI agents locally and connect them to the cloud orchestrator for distributed task execution.

## Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/dmquant/agent-route-node/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/dmquant/agent-route-node.git ~/.agent-route/node
cd ~/.agent-route/node
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Usage

### 1. Get an invite key

Ask your network admin to generate an invite key (`ik_...`) from the **Edge Nodes** page in the Agent Route dashboard.

### 2. Register your node

```bash
agent-route-node register \
  --worker-url https://agent-route.example.workers.dev \
  --invite-key ik_your_invite_key_here \
  --name "My Mac Studio"
```

This will:
- Discover your available CLI agents
- Register your node with the cloud orchestrator
- Generate a unique node token (`nk_...`) for authentication
- Write a complete config to `~/.agent-route/.env`

### 3. Configure hands

Edit `~/.agent-route/.env` to enable/disable hands:

```env
# Set to true/false to enable/disable each hand
ENABLE_GEMINI_CLI=true
ENABLE_CLAUDE_REMOTE_CONTROL=true
ENABLE_CODEX_SERVER=true
ENABLE_OLLAMA_API=false
ENABLE_MFLUX_IMAGE=false
ENABLE_VANE_SEARCH=false
ENABLE_OPENCODE=false
```

Only enabled hands will register with the orchestrator and receive tasks.

### 4. Start the node

```bash
agent-route-node start
```

### 5. Check status

```bash
agent-route-node status
```

### 6. Stop the node

```bash
agent-route-node stop
# or from install dir:
cd ~/.agent-route/node && ./stop.sh
```

### 7. Update to latest version

```bash
cd ~/.agent-route/node && git pull && ./stop.sh && ./start.sh
```

### 8. Diagnostics

```bash
# Test all hands
python3 test_hands.py

# Test a specific hand
python3 test_hands.py gemini

# Debug a hand (shows exact command, env, auth)
python3 debug_hand.py claude

# Watch task execution log
tail -f ~/.agent-route/task_puller.log
```

## Available Hands

| Hand | Type | Requires | Enable Flag |
|------|------|----------|-------------|
| **gemini** | CLI | `npx gemini` (Node.js) | `ENABLE_GEMINI_CLI` |
| **claude** | CLI | `npx @anthropic-ai/claude-code` (Node.js) | `ENABLE_CLAUDE_REMOTE_CONTROL` |
| **codex** | CLI | `npx codex` (Node.js) | `ENABLE_CODEX_SERVER` |
| **ollama** | HTTP | Ollama running locally | `ENABLE_OLLAMA_API` |
| **mflux** | HTTP | MFLUX on Apple Silicon | `ENABLE_MFLUX_IMAGE` |
| **vane** | HTTP | Vane search instance | `ENABLE_VANE_SEARCH` |
| **opencode** | HTTP | [opencode](https://opencode.ai) running in server mode | `ENABLE_OPENCODE` |

## Configuration

All config lives in `~/.agent-route/.env`:

```env
# ─── Agent Enable Flags ───
ENABLE_GEMINI_CLI=true
ENABLE_CLAUDE_REMOTE_CONTROL=true
ENABLE_CODEX_SERVER=true
ENABLE_OLLAMA_API=false
ENABLE_MFLUX_IMAGE=false
ENABLE_VANE_SEARCH=false

# ─── Node Identity (set by registration) ───
CF_WORKER_URL=https://agent-route.example.workers.dev
NODE_TOKEN=nk_...
NODE_ID=...
NODE_URL=http://localhost:8017
NODE_KEY=dcpn_...

# ─── Ollama (optional) ───
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2

# ─── Vane AI Search (optional) ───
VANE_URL=http://localhost:3000
VANE_CHAT_MODEL=gemma4:26b
VANE_EMBED_MODEL=nomic-embed-text:latest

# ─── OpenCode server mode (optional) ───
# Run: opencode serve --port 4096   (set OPENCODE_SERVER_PASSWORD on the server)
OPENCODE_URL=http://localhost:4096
OPENCODE_USERNAME=opencode
OPENCODE_PASSWORD=
OPENCODE_MODEL=                       # e.g. anthropic/claude-3-5-sonnet
OPENCODE_AGENT=                       # optional, scopes tool access
OPENCODE_TIMEOUT_S=600

# ─── Concurrency ───
NODE_MAX_CONCURRENT=3                 # Max parallel tasks per node
```

## How It Works

Your edge node:
- Registers enabled hands with the Agent Route cloud orchestrator
- Polls for pending tasks every **15 seconds** (non-blocking)
- Executes up to `NODE_MAX_CONCURRENT` (default **3**) tasks in parallel via
  asyncio coroutines gated by a semaphore — a single slow CLI no longer
  blocks polling or other tasks
- Downloads workspace files from R2 before execution (cross-node context)
- Uploads output files back to R2 after execution
- Posts results back to the orchestrator
- Sends heartbeats every 30s with load stats
- Skips polling when already at concurrency capacity (avoids the worker
  marking unrunnable tasks as `running`)

## Using Your Token

After registration, you receive a node token (`nk_...`). This works as SSO across:

- **Agent Route Frontend** — enter the token to login
- **Infinite Research** — same token authenticates research sessions
- **API access** — `curl -H "X-API-Key: nk_..." https://worker/api/...`

## For Admins

To manage the node fleet:

1. Login to the Agent Route dashboard with your admin key (`sk_admin_...`)
2. Go to **Edge Nodes** page
3. Generate **Invite Keys** for new nodes
4. Expand any node card to:
   - Toggle individual hands on/off
   - Adjust max concurrent tasks
   - Rename the node
   - Suspend/resume the node
   - Reset degraded status
   - Delete the node

## Requirements

- Python 3.11+
- At least one CLI agent installed:
  - `npx @anthropic-ai/claude-code` (Claude Code)
  - `npx gemini` (Gemini CLI)  
  - `npx codex` (Codex CLI)
  - Ollama running locally (optional)
  - Vane search instance (optional)
