"""agy's node-global MCP registry is scrubbed of node credentials on every agy task.

ai-institute IKG-278 fafc3d2d-2945-41e0-af8e-6b05729846f6 (2026-10-07): a 09-23 literal nk_ in
~/.gemini/config/mcp_config.json outlived d2ee067 and agy kept presenting it after revocation.
"""
import json
import os
import stat

from app import mcp_config


def _write(home, servers):
    p = home / ".gemini" / "config" / "mcp_config.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": servers}))
    return p


def _off(monkeypatch, home):
    monkeypatch.setenv("HOME", str(home))  # never the live registry
    for v in ("AI_INSTITUTE_MCP_URL", "AI_INSTITUTE_MCP_KEY", "ROS_GATEWAY_MCP_URL", "ROS_GATEWAY_MCP_KEY"):
        monkeypatch.delenv(v, raising=False)


def test_a_node_token_in_a_header_is_removed_even_when_mcp_is_off(tmp_path, monkeypatch):
    _off(monkeypatch, tmp_path)
    p = _write(tmp_path, {
        "ai-institute": {"disabled": False, "serverUrl": "https://i/api/mcp", "headers": {"X-Admin-Token": "nk_old-revoked-value-000000"}},
        "ros-gateway": {"disabled": False, "serverUrl": "https://g/mcp", "headers": {"Authorization": "Bearer nk_old-revoked-value-111111"}},
    })
    mcp_config.prepare("agy", str(tmp_path / "ws"))
    text = p.read_text()
    assert "nk_" not in text
    d = json.loads(text)["mcpServers"]
    assert d["ai-institute"]["disabled"] is True and d["ros-gateway"]["disabled"] is True
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_other_servers_and_real_keys_are_left_alone(tmp_path, monkeypatch):
    # control: the scrub is by credential class, not by server name
    _off(monkeypatch, tmp_path)
    other = {"disabled": False, "serverUrl": "https://x/mcp", "headers": {"Authorization": "Bearer rosnode_real"}}
    p = _write(tmp_path, {"someone-else": other})
    before = p.read_text()
    mcp_config.prepare("agy", str(tmp_path / "ws"))
    assert p.read_text() == before


def test_other_hands_do_not_touch_the_agy_registry(tmp_path, monkeypatch):
    _off(monkeypatch, tmp_path)
    p = _write(tmp_path, {"ai-institute": {"headers": {"X-Admin-Token": "nk_old-revoked-value-000000"}}})
    mcp_config.prepare("codex", str(tmp_path / "ws"))
    assert "nk_old-revoked-value-000000" in p.read_text()
