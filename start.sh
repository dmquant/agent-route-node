#!/bin/bash
set -e
cd "$(dirname "$0")"

# Create workspaces directory
AGENT_ROUTE_HOME="${AGENT_ROUTE_HOME:-$HOME/.agent-route}"
mkdir -p "$AGENT_ROUTE_HOME/workspaces/sessions"

# Install dependencies if needed
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

# Load .env — check local first, then monorepo root, then data dir
if [ -f ".env" ]; then
    ENV_FILE=".env"
elif [ -f "../../.env" ]; then
    ENV_FILE="../../.env"
elif [ -f "$AGENT_ROUTE_HOME/.env" ]; then
    ENV_FILE="$AGENT_ROUTE_HOME/.env"
else
    echo "[api_bridge] No .env found. Run: agent-route-node register"
    exit 1
fi

set -a
source "$ENV_FILE"
set +a

# Extract port from NODE_URL
PORT="${NODE_URL##*:}"
PORT="${PORT:-8017}"

# --reload is dev-only — uvicorn's reloader supervisor needs a tty,
# so backgrounded runs (nohup, launchd, systemd) exit on first SIGHUP.
# Default off; opt in with `start.sh --dev` or API_BRIDGE_DEV=1.
RELOAD_FLAG=""
if [ "${1:-}" = "--dev" ] || [ "${API_BRIDGE_DEV:-}" = "1" ]; then
    RELOAD_FLAG="--reload"
fi

echo "[api_bridge] env=$ENV_FILE | port=$PORT | data=$AGENT_ROUTE_HOME${RELOAD_FLAG:+ | reload=on}"
export AGENT_ROUTE_HOME
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT" $RELOAD_FLAG
