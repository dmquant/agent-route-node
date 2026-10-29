"""Centralized path configuration for standalone or monorepo installs."""
import os


def get_data_dir() -> str:
    """Return the data directory. Standalone: ~/.agent-route, monorepo: ../../"""
    d = os.getenv("AGENT_ROUTE_HOME", os.path.expanduser("~/.agent-route"))
    os.makedirs(d, exist_ok=True)
    return d


def get_workspaces_dir() -> str:
    # Under the user split (AGENT_ROUTE_HAND_USER) the data dir is the node user's
    # 0700 home, which the hand user cannot enter; workspaces must then live under
    # the launcher's workspace_root, e.g. /Users/Shared/agentroute-ws.
    override = os.getenv("AGENT_ROUTE_WORKSPACES_DIR", "").strip()
    d = override or os.path.join(get_data_dir(), "workspaces", "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def get_db_path() -> str:
    return os.path.join(get_data_dir(), "sessions.db")


def get_env_path() -> str:
    """Return the .env file path.
    Priority: data dir (~/.agent-route/.env) > monorepo root > local (code dir)
    """
    # 1. Data directory — the canonical location for standalone installs
    data_env = os.path.join(get_data_dir(), ".env")
    if os.path.exists(data_env):
        return data_env
    # 2. Monorepo root (../../.env from packages/api_bridge/)
    monorepo = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".env")
    if os.path.exists(monorepo):
        return monorepo
    # 3. Local to code dir (fallback)
    local = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(local):
        return local
    # Default: data dir (will be created by register)
    return data_env
