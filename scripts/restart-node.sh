#!/bin/zsh
# Restart the edge node daemon.
#
# There is no supervisor: nothing restarts this process if it dies or if the machine reboots, and
# the environment it inherits is whatever the launching shell had. On 2026-09-09 a restart from a
# shell without the profile left every hand unresolvable for twenty minutes -- and the failure was
# invisible, because /api/agents answered 200 the whole time. resolve_cli_path() no longer depends
# on PATH, so that specific fault is fixed in code; this script exists so a restart is reproducible
# rather than retyped, and so the verification below is not optional.
set -e
cd "$HOME/.agent-route/node"

OLD=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port 8017" | head -1 || true)
if [ -n "$OLD" ]; then
  echo "stopping $OLD"
  kill -TERM "$OLD" 2>/dev/null || true
  for i in {1..10}; do
    pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port 8017" >/dev/null || break
    sleep 1
  done
fi

nohup "$HOME/.agent-route/node/.venv/bin/python3" \
      "$HOME/.agent-route/node/.venv/bin/uvicorn" \
      app.main:app --host 0.0.0.0 --port 8017 \
      > /tmp/agent-route-node.log 2>&1 &
sleep 6

NEW=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port 8017" | head -1 || true)
[ -n "$NEW" ] || { echo "FAILED: daemon did not start"; exit 1; }
echo "started $NEW"

# Verify the HAND, not the endpoint. /api/agents returns 200 whether or not any CLI is resolvable.
.venv/bin/python3 - <<'PY'
import asyncio, os, sys
sys.path.insert(0, os.path.expanduser("~/.agent-route/node"))
from app.hands.opencode_hand import OpencodeHand
ok = asyncio.run(OpencodeHand().health_check())
print(f"  opencode hand health_check: {ok}")
raise SystemExit(0 if ok else 1)
PY
echo "restart verified"
