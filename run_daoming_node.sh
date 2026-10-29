#!/bin/bash
export HOME=/Users/daoming
export USER=daoming
export PATH="/opt/ros-runtime/npm/bin:/opt/ros-runtime/node/bin:/opt/ros-runtime/bin:/Users/daoming/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export NPM_CONFIG_PREFIX=/opt/ros-runtime/npm
export AGENT_ROUTE_HOME=/Users/daoming/.agent-route
cd /Users/daoming/agentroute-split-d455a72
exec /Users/daoming/.agent-route/node/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8017
