#!/bin/zsh
# Install the launchd supervisor for the edge node daemon.
#
# Division of labour: launchd is the muscle -- it restarts the process when it exits and starts it
# at login, which a coordination session cannot do because it is not running most of the time.
# scripts/supervise.py is the judgement -- it decides whether a running daemon is actually working,
# which launchd cannot do because a live process and a working hand are different things. On
# 2026-09-09 the daemon was up and answering 200 while every hand was unresolvable.
#
# User-level LaunchAgent: no sudo, starts at login. Deliberately not a LaunchDaemon -- these are
# workstations someone logs into, and a root daemon is a bigger claim than the problem needs.
set -e
LABEL=com.dmquant.agent-route-node
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
NODE="$HOME/.agent-route/node"
LOG="$HOME/Library/Logs/agent-route-node.log"

[ -x "$NODE/.venv/bin/uvicorn" ] || { echo "FAILED: $NODE/.venv/bin/uvicorn not found"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# Stop anything started by hand, or launchd will race it and there will be two daemons.
OLD=$(pgrep -f "uvicorn app.main:app --host 0.0.0.0 --port 8017" || true)
if [ -n "$OLD" ]; then echo "stopping manually-started daemon $OLD"; kill -TERM $OLD 2>/dev/null || true; sleep 3; fi
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$NODE/.venv/bin/python3</string>
    <string>$NODE/.venv/bin/uvicorn</string>
    <string>app.main:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8017</string>
  </array>
  <key>WorkingDirectory</key><string>$NODE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- A crashloop must not hammer the worker's registration endpoint. -->
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>$HOME</string>
    <!-- resolve_cli_path() no longer needs this, but a daemon should not depend on that being
         true forever. Belt and braces after the 2026-09-09 outage. -->
    <key>PATH</key><string>$HOME/.opencode/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLISTEOF

launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 7
launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid) " | sed 's/^/  /'
echo "installed: $PLIST"
