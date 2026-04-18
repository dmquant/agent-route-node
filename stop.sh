#!/bin/bash
# Load NODE_URL to find port
ENV_FILE=""
if [ -f "$(dirname "$0")/.env" ]; then ENV_FILE="$(dirname "$0")/.env"
elif [ -f "$(dirname "$0")/../../.env" ]; then ENV_FILE="$(dirname "$0")/../../.env"
elif [ -f "${AGENT_ROUTE_HOME:-$HOME/.agent-route}/.env" ]; then ENV_FILE="${AGENT_ROUTE_HOME:-$HOME/.agent-route}/.env"
fi
[ -n "$ENV_FILE" ] && source "$ENV_FILE" 2>/dev/null
PORT="${NODE_URL##*:}"
PORT="${PORT:-8017}"

PIDS=$(lsof -ti :"$PORT" 2>/dev/null)
if [ -n "$PIDS" ]; then
    echo "$PIDS" | xargs kill 2>/dev/null
    echo "[api_bridge] Stopped (port $PORT)"
else
    echo "[api_bridge] Not running (port $PORT)"
fi
