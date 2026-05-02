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
| **opencode** | CLI | `opencode` binary on PATH ([install](https://opencode.ai/install)) | `ENABLE_OPENCODE` |

### Adding the **opencode** hand

```bash
# 1. Install the opencode CLI
curl -fsSL https://opencode.ai/install | bash

# 2. Configure auth for whichever provider you want to use
opencode auth set anthropic           # or openai, openrouter, etc.

# 3. Pick a default model in ~/.agent-route/.env
echo 'ENABLE_OPENCODE=true'                                >> ~/.agent-route/.env
echo 'OPENCODE_MODEL=anthropic/claude-3-5-sonnet'          >> ~/.agent-route/.env

# 4. Restart the node
agent-route-node stop && agent-route-node start
```

opencode runs as a per-task subprocess with `cwd` set to the session
workspace, so its file tools (`write`, `edit`, `read`, `bash`) operate
inside the agent-route session directory. Files persist across nodes via
the puller's R2 sync — same as `claude` / `gemini` / `codex`.

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
ENABLE_OPENCODE=false

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

# ─── OpenCode CLI (optional) ───
# Auth: ensure `opencode auth set <provider>` was run on this machine.
OPENCODE_MODEL=                       # e.g. anthropic/claude-3-5-sonnet
OPENCODE_AGENT=                       # optional, scopes tool access
OPENCODE_TIMEOUT_S=600                # subprocess wall timeout

# ─── Default models (optional — overridden per task) ───
GEMINI_DEFAULT_MODEL=                 # e.g. gemini-2.5-pro / gemini-2.5-flash
CLAUDE_DEFAULT_MODEL=                 # e.g. claude-sonnet-4-7 / claude-haiku-4-5
CODEX_DEFAULT_MODEL=                  # e.g. gpt-5-codex

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
- Sends heartbeats every 30s with **load + per-hand availability**
- Skips polling when already at concurrency capacity (avoids the worker
  marking unrunnable tasks as `running`)

## Reliability — rate-limit detection & fallback

The node parses each CLI's rate-limit signature, persists a cooldown to
disk, and transparently falls back to a sibling hand within a coding
group. Surviving a quota burn no longer requires manual intervention.

### Per-CLI parsers

| CLI | Recognized signatures | Reset source |
|---|---|---|
| **gemini** | `TerminalQuotaError ... Your quota will reset after Xh Ym Zs` · `429 RESOURCE_EXHAUSTED` | duration |
| **claude** | `Claude AI usage limit reached. Your usage limit will reset at HH:MM (TZ)` · `rate_limit_error` · `Overloaded` | wall-clock + duration |
| **codex** | `rate_limit_exceeded ... Please try again in Xs` · `Retry-After: N` · `429` | duration |

Default fallback when a quota signature is detected but no time can be
parsed: **5 hours**. This used to be 1h, which caused us to re-hit
15-hour gemini quotas 14 hours early.

### Persistent cooldown

Cooldowns are written to `~/.agent-route/rate_limits.json`:

```json
{
  "gemini": {
    "until": 1777793591,
    "reason": "quota_exhausted",
    "marked_at": 1777737996
  }
}
```

The file is loaded on every startup. A node restart (intentional or
otherwise) will not reset an active cooldown.

### Automatic fallback chain

The three coding CLIs are interchangeable for most prompts and form a
fallback group:

| Primary | Fallback order |
|---|---|
| `gemini` | `claude` → `codex` |
| `claude` | `gemini` → `codex` |
| `codex` | `claude` → `gemini` |

When the requested hand is in cooldown (or not enabled, or unregistered),
the puller picks the first available hand in the chain and runs the task
there. Other hands (`vane`, `mflux`, `ollama`, `opencode`) have **no**
default fallback — they're capability-specific.

To opt out of fallback for a specific task and have it fail-fast on the
requested hand, set `meta.fallback = false` in the task payload at the
worker side. (The puller accepts both `task.meta.fallback` and a
top-level `task.fallback`.)

When ALL hands in the chain are exhausted, the puller posts `exit_code=429`
with a structured prefix the worker can detect:

```
[RATE_LIMITED hand=gemini all_exhausted=true tried=gemini,claude,codex]
All candidate hands are on cooldown.
  gemini: cool until 2026-05-03T12:34:56 (reason=quota_exhausted)
  claude: cool until 2026-05-03T14:00:00 (reason=usage_limit_wall_clock)
  codex: cool until 2026-05-02T22:30:00 (reason=rate_limit_duration)
```

### Inspecting state

```bash
# Per-hand availability snapshot (live)
curl http://localhost:8017/api/hands/status | jq

# Manually clear a cooldown (ops escape hatch)
curl -X POST http://localhost:8017/api/hands/gemini/cooldown/clear
```

The same status payload is sent on every heartbeat under `handStatus`,
so the cloud worker can route around an exhausted hand without first
needing to send a doomed task.

## Per-task model selection

The three CLI hands accept a `model` parameter on every execution. The
puller forwards `task.model` (or `task.meta.model`) from the worker
straight into the CLI invocation:

| Hand | CLI flag | Examples |
|---|---|---|
| `gemini` | `--model <name>` | `gemini-2.5-pro` (default), `gemini-2.5-flash` |
| `claude` | `--model <name>` | `claude-sonnet-4-7` (default), `claude-haiku-4-5`, `claude-opus-4-7` |
| `codex` | `-m <name>` | `gpt-5-codex` (default), other OpenAI-compatible models |

When omitted, each CLI uses its compiled-in default unless overridden by
`{GEMINI,CLAUDE,CODEX}_DEFAULT_MODEL` in `~/.agent-route/.env`.

Resolution order: per-request `model` parameter → env default → CLI default.

Calling directly via `/execute`:

```bash
curl -sX POST http://localhost:8017/execute \
  -H 'Content-Type: application/json' \
  -d '{"client":"claude","prompt":"summarise this repo","model":"claude-haiku-4-5"}'
```

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
  - `opencode` (OpenCode CLI — multi-provider; install via `curl -fsSL https://opencode.ai/install | bash`)
  - Ollama running locally (optional)
  - Vane search instance (optional)
