"""Centralized path configuration for standalone or monorepo installs."""
import os


def get_data_dir() -> str:
    """Return the data directory. Standalone: ~/.agent-route, monorepo: ../../"""
    d = os.getenv("AGENT_ROUTE_HOME", os.path.expanduser("~/.agent-route"))
    os.makedirs(d, exist_ok=True)
    return d


def get_workspaces_dir() -> str:
    d = os.path.join(get_data_dir(), "workspaces", "sessions")
    os.makedirs(d, exist_ok=True)
    return d


def get_db_path() -> str:
    return os.path.join(get_data_dir(), "sessions.db")


def get_env_path() -> str:
    """Return the .env file path."""
    # Check local .env first (standalone), then monorepo root
    local = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if os.path.exists(local):
        return local
    monorepo = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".env")
    if os.path.exists(monorepo):
        return monorepo
    return local  # default to local
