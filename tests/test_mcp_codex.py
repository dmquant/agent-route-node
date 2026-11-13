"""ARC-209: _prepare_codex mounts scheme-less custom-header servers via env_http_headers."""
import unittest

from app import mcp_config


def _cfg(name, header, scheme, var):
    return {"name": name, "url": "https://x.invalid/mcp", "header": header,
            "scheme": scheme, "key_var": var, "key": "k"}


class PrepareCodexTests(unittest.TestCase):
    def test_x_api_key_uses_env_http_headers(self):
        plan = mcp_config._prepare_codex([_cfg("ai-institute", "X-API-Key", "", "AI_KEY")], "/tmp/w")
        self.assertIn('mcp_servers.ai-institute.env_http_headers={"X-API-Key"="AI_KEY"}', plan.args)
        self.assertNotIn("declined", plan.note)
        self.assertEqual(plan.env, {"AI_KEY": "k"})

    def test_bearer_unchanged(self):
        plan = mcp_config._prepare_codex([_cfg("g", "Authorization", "Bearer ", "G_KEY")], "/tmp/w")
        self.assertIn('mcp_servers.g.bearer_token_env_var="G_KEY"', plan.args)

    def test_schemed_non_bearer_still_declined(self):
        plan = mcp_config._prepare_codex([_cfg("s", "X-Admin-Token", "Token ", "S_KEY")], "/tmp/w")
        self.assertEqual(plan.args, [])
        self.assertIn("declined s", plan.note)


if __name__ == "__main__":
    unittest.main()
