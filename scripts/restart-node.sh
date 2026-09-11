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

# Drain before killing anything. A task in flight when the daemon dies never posts
# its callback, so agent-route leaves the row `running` for ever -- 13 orphans across
# SG and gubei12 on 2026-09-11, up to 140 minutes old, against zero live processes.
# Whoever dispatched them is still waiting. Nothing reaps them: max_wait_ms is 0 on
# those rows, so no server-side deadline applies either.
#
# Every test below is written as an `if`, not `[ ... ] && ...`. Under `set -e` a
# trailing test that returns false ends the script, so the && form would abort the
# restart precisely when there was nothing to drain.
#
# DRAIN_WAIT_S=0 skips the wait, for when a node is wedged and waiting is pointless.
inflight() {
  curl -s -m 5 "http://localhost:8017/api/tasks/inflight" 2>/dev/null \
    | sed -n 's/.*"inflight"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p'
}

DRAIN_WAIT_S=${DRAIN_WAIT_S:-180}
if [ "$DRAIN_WAIT_S" -gt 0 ]; then
  N=$(inflight || true)
  if [ -n "$N" ] && [ "$N" != "0" ]; then
    echo "draining: $N task(s) in flight, waiting up to ${DRAIN_WAIT_S}s"
    WAITED=0
    while [ "$WAITED" -lt "$DRAIN_WAIT_S" ]; do
      sleep 5
      WAITED=$((WAITED+5))
      N=$(inflight || true)
      if [ -z "$N" ]; then
        echo "daemon stopped answering while draining — continuing"
        break
      fi
      if [ "$N" = "0" ]; then
        echo "drained after ${WAITED}s"
        break
      fi
    done
    if [ -n "$N" ] && [ "$N" != "0" ]; then
      echo "WARNING: ${N} still in flight after ${WAITED}s — restarting anyway; those rows will orphan"
    fi
  fi
fi

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

# A freshly kickstarted daemon needs a moment before it registers hands. Running the
# supervisor into that window reports a node broken that is merely still starting.
for i in {1..20}; do
  if curl -s -m 3 http://localhost:8017/api/hands/health >/dev/null 2>&1; then break; fi
  sleep 2
done

.venv/bin/python3 scripts/supervise.py
