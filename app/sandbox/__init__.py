"""Sandbox Pool — workspace provisioning and lifecycle.

Key Anthropic insight: workspaces are cattle, not pets.
provision() creates isolated, disposable workspaces.
destroy() cleans them up. No workspace is irreplaceable.

This replaces the ad-hoc workspace provisioning in session_store.py
with a formal pool manager with quotas, TTL, and metadata.
"""

from app.sandbox.pool import SandboxPool, SandboxInfo, sandbox_pool

__all__ = ["SandboxPool", "SandboxInfo", "sandbox_pool"]
