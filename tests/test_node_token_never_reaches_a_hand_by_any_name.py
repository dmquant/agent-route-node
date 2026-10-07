"""A node's transport credential never reaches a hand, under ANY variable name.

ai-institute IKG-274 2400a4e6-1218-427a-ac21-2d27b0450415 (2026-10-07): f1aeccc stripped NODE_TOKEN by
name, but the same nk_ value also sat in AI_INSTITUTE_MCP_KEY / ROS_GATEWAY_MCP_KEY (mcp_config fell
back to NODE_TOKEN and injected it), so a codex hand ran `env` and printed it.
"""
import os

from app import mcp_config
from app.hands import base


def test_node_token_value_is_dropped_under_another_name(monkeypatch):
    monkeypatch.setenv("NODE_TOKEN", "nk_the-node-token-value")
    monkeypatch.setenv("AI_INSTITUTE_MCP_KEY", "nk_the-node-token-value")
    monkeypatch.setenv("SOME_ALIAS", "nk_the-node-token-value")
    env = base.get_cli_env("/tmp")
    assert "nk_the-node-token-value" not in "\n".join(env.values())
    assert "AI_INSTITUTE_MCP_KEY" not in env and "SOME_ALIAS" not in env


def test_node_credential_shaped_values_are_dropped(monkeypatch):
    monkeypatch.delenv("NODE_TOKEN", raising=False)
    monkeypatch.setenv("ROS_GATEWAY_MCP_KEY", "nk_some-other-node")
    monkeypatch.setenv("WHATEVER_KEY", "dcpn_0123456789abcdef")
    env = base.get_cli_env("/tmp")
    assert "ROS_GATEWAY_MCP_KEY" not in env and "WHATEVER_KEY" not in env


def test_real_mcp_keys_still_reach_the_hand(monkeypatch):
    # control: the strip is by credential, not by variable name
    monkeypatch.setenv("NODE_TOKEN", "nk_node")
    monkeypatch.setenv("AI_INSTITUTE_MCP_KEY", "mcpk_a-real-mcp-key")
    monkeypatch.setenv("ROS_GATEWAY_MCP_KEY", "rosnode_a-real-gateway-key")
    env = base.get_cli_env("/tmp")
    assert env.get("AI_INSTITUTE_MCP_KEY") == "mcpk_a-real-mcp-key"
    assert env.get("ROS_GATEWAY_MCP_KEY") == "rosnode_a-real-gateway-key"


def test_mcp_never_falls_back_to_the_node_token(monkeypatch):
    for v in ("AI_INSTITUTE_MCP_KEY", "ROS_GATEWAY_MCP_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("NODE_TOKEN", "nk_node")
    monkeypatch.setenv("AI_INSTITUTE_MCP_URL", "https://institute.example/api/mcp")
    monkeypatch.setenv("ROS_GATEWAY_MCP_URL", "https://gateway.example/mcp")
    assert mcp_config.settings() is None
    assert mcp_config.gateway_settings() is None
    # the nk_ refusal would mask a restored fallback, so prove the fallback itself is gone with a
    # NODE_TOKEN the shape check cannot catch
    monkeypatch.setenv("NODE_TOKEN", "legacy-token-without-prefix")
    assert mcp_config.settings() is None
    assert mcp_config.gateway_settings() is None


def test_mcp_refuses_an_nk_key_set_explicitly(monkeypatch):
    monkeypatch.setenv("AI_INSTITUTE_MCP_URL", "https://institute.example/api/mcp")
    monkeypatch.setenv("AI_INSTITUTE_MCP_KEY", "nk_explicitly-configured")
    monkeypatch.setenv("ROS_GATEWAY_MCP_URL", "https://gateway.example/mcp")
    monkeypatch.setenv("ROS_GATEWAY_MCP_KEY", "nk_explicitly-configured")
    assert mcp_config.settings() is None
    assert mcp_config.gateway_settings() is None
    # control: an MCP-class key is still accepted
    monkeypatch.setenv("ROS_GATEWAY_MCP_KEY", "rosnode_ok")
    assert mcp_config.gateway_settings() is not None
