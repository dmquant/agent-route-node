#!/bin/bash
set -e

# ─── Agent Route Edge Node Installer ───
# One-line install: curl -sSL https://raw.githubusercontent.com/dmquant/agent-route-node/main/install.sh | bash

INSTALL_DIR="${AGENT_ROUTE_HOME:-$HOME/.agent-route}"
REPO_URL="${AGENT_ROUTE_NODE_REPO:-https://github.com/dmquant/agent-route-node.git}"

echo "╔══════════════════════════════════════════════════╗"
echo "║       Agent Route Edge Node Installer            ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required. Install it first."
    exit 1
fi

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo $PY_VER | cut -d. -f1)
PY_MINOR=$(echo $PY_VER | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]); then
    echo "Error: Python 3.11+ required (found $PY_VER)"
    exit 1
fi

echo "Python $PY_VER detected"

# Create install directory
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Clone or update
if [ -d "$INSTALL_DIR/node" ]; then
    echo "Updating existing installation..."
    cd "$INSTALL_DIR/node"
    git pull --ff-only 2>/dev/null || true
else
    echo "Downloading agent-route-node..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/node" 2>/dev/null || {
        # Fallback: if repo doesn't exist yet, create from template
        mkdir -p "$INSTALL_DIR/node"
        echo "Note: Using local template (repo not yet published)"
    }
    cd "$INSTALL_DIR/node"
fi

# Create venv and install
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Installing dependencies..."
.venv/bin/pip install -q -e . 2>/dev/null || .venv/bin/pip install -q -r requirements.txt

# Create default .env if not exists
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cat > "$INSTALL_DIR/.env" << 'ENVEOF'
# Agent Enable Flags
ENABLE_GEMINI_CLI=true
ENABLE_CLAUDE_REMOTE_CONTROL=true
ENABLE_CODEX_SERVER=true
ENABLE_OLLAMA_API=true
ENABLE_MFLUX_IMAGE=false

# Node Config (populated by: agent-route-node register)
NODE_URL=http://localhost:8017

# Ollama (optional)
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2
ENVEOF
    echo "Created $INSTALL_DIR/.env"
fi

# Create convenience wrapper
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/agent-route-node" << BINEOF
#!/bin/bash
export AGENT_ROUTE_HOME="$INSTALL_DIR"
cd "$INSTALL_DIR/node"
exec .venv/bin/python -m app.cli "\$@"
BINEOF
chmod +x "$HOME/.local/bin/agent-route-node"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Installation complete!"
echo ""
echo "  Data directory: $INSTALL_DIR"
echo "  Binary: ~/.local/bin/agent-route-node"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Register with a worker:"
echo "     agent-route-node register \\"
echo "       --worker-url https://your-worker.workers.dev \\"
echo "       --admin-key sk_admin_xxx"
echo ""
echo "  2. Start the node:"
echo "     agent-route-node start"
echo ""
echo "  3. Check status:"
echo "     agent-route-node status"
echo ""
echo "  Make sure ~/.local/bin is in your PATH:"
echo "     export PATH=\"\$HOME/.local/bin:\$PATH\""
echo "═══════════════════════════════════════════════════"
