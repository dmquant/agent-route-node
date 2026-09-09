#!/bin/zsh
# Restart the edge node daemon, and verify the HAND rather than the endpoint.
#
# /api/agents returns 200 whether or not any CLI is resolvable -- on 2026-09-09 it answered 200 for
# twenty minutes while every dispatch failed in under a second. A restart is not verified until a
# hand reports healthy.
set -e
LABEL=com.dmquant.agent-route-node
NODE="$HOME/.agent-route/node"
cd "$NODE"

if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  # launchd owns the process; kicking it by hand would race the supervisor.
  echo "supervised by launchd — kickstarting"
  launchctl kickstart -k "gui/$(id -u)/$LABEL"
else
  echo "no launchd supervisor (run scripts/install-supervisor.sh) — starting manually"
  OLD=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port 8017" | head -1 || true)
  [ -n "$OLD" ] && { kill -TERM "$OLD" 2>/dev/null || true; sleep 3; }
  nohup "$NODE/.venv/bin/python3" "$NODE/.venv/bin/uvicorn" \
        app.main:app --host 0.0.0.0 --port 8017 \
        > "$HOME/Library/Logs/agent-route-node.log" 2>&1 &
fi

for i in {1..15}; do
  PID=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port 8017" | head -1 || true)
  [ -n "$PID" ] && break
  sleep 1
done
[ -n "$PID" ] || { echo "FAILED: daemon did not come back"; exit 1; }
echo "running: $PID"

.venv/bin/python3 scripts/supervise.py
