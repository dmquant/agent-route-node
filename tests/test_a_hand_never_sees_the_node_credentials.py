"""A dispatched hand must not receive the node's own credentials.

Measured on gubei12 2026-09-24: get_cli_env merged the daemon's environment,
so all 21 keys of the node's .env reached every hand, among them NODE_KEY and
NODE_TOKEN (the node's identity with agent-route). Operator decree
c8f658ec-72a3-4f2f-8572-0c9ee6b33da7 asks for a sanitized hand environment.

The MCP keys stay on purpose: hand configs write `${AI_INSTITUTE_MCP_KEY}`
headers that the hand resolves from its own environment.
"""
from app.hands import base


def test_node_credentials_are_stripped_and_mcp_keys_kept(monkeypatch):
    monkeypatch.setenv("NODE_TOKEN", "node-token-value")
    monkeypatch.setenv("NODE_KEY", "dcpn_node-key-value")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-value")
    monkeypatch.setenv("AI_INSTITUTE_MCP_KEY", "institute-key-value")
    env = base.get_cli_env("/tmp")
    # control: the environment really was built from ours
    assert env.get("AI_INSTITUTE_MCP_KEY") == "institute-key-value"
    assert env.get("HOME") and env.get("PATH")
    for k in ("NODE_TOKEN", "NODE_KEY", "ADMIN_API_KEY"):
        assert k not in env, k
    # and no VALUE of theirs survives under another name
    blob = "\n".join(env.values())
    for v in ("node-token-value", "dcpn_node-key-value", "admin-value"):
        assert v not in blob, v
