"""A hand must not be handed a server the endpoint has already refused.

Measured live on sg01 2026-09-22 23:2x, minutes after the operator set the URL:

    AI_INSTITUTE_MCP_URL    set
    AI_INSTITUTE_MCP_KEY    set
    AI_INSTITUTE_MCP_HEADER NOT set  -> header defaults to Authorization
                                     -> scheme defaults to "Bearer " because the
                                        header is Authorization
    as the node would send it                 HTTP 401
    CONTROL, a zeroed key of the same length  HTTP 401
    the SAME key as X-Admin-Token             HTTP 200, 57 tools

So the node went from a clean absence -- settings() returned None and no hand got
a server -- to every hand getting a server that fails on every call, and the 401
carrying the right credential is byte-identical to the 401 carrying a zeroed one.

The asymmetry this pins is epistates 49f62ef2-3ec8-4785-acac-0d3657e3ad23 pointed
at a refusal: an explicit 401 is the endpoint STATING it will not serve this
credential; a timeout is the absence of evidence. Refusing on the second would
take a working node off MCP because a network blipped.
"""
import io
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import mcp_config   # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cfg(**over):
    base = {"name": "ai-institute", "url": "https://example.invalid/api/mcp",
            "key": "nk_" + "a" * 64, "header": "Authorization", "scheme": "Bearer ",
            "key_var": "AI_INSTITUTE_MCP_KEY", "url_var": "AI_INSTITUTE_MCP_URL",
            "hands": list(mcp_config.SUPPORTED_HANDS)}
    base.update(over)
    return base


class TestAServerThat401sIsWithheld(unittest.TestCase):

    def setUp(self):
        mcp_config._CRED_VERDICT.clear()
        self._real = urllib.request.urlopen
        self.calls = []

    def tearDown(self):
        urllib.request.urlopen = self._real
        mcp_config._CRED_VERDICT.clear()

    def _patch(self, outcome):
        def fake(req, timeout=None):
            self.calls.append(req.full_url)
            if isinstance(outcome, Exception):
                raise outcome
            return _Resp(b'{"jsonrpc":"2.0","id":1,"result":{}}')
        urllib.request.urlopen = fake

    # --- the refusal, which is the only thing allowed to disqualify -------------

    def test_401_is_a_refusal(self):
        self._patch(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "refused")

    def test_403_is_a_refusal(self):
        self._patch(urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "refused")

    def test_200_is_ok(self):
        self._patch(None)
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "ok")

    # --- everything else is UNKNOWN, and unknown must not disqualify ------------

    def test_a_timeout_is_not_a_refusal(self):
        self._patch(TimeoutError("timed out"))
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "unknown")

    def test_a_5xx_is_not_a_refusal(self):
        self._patch(urllib.error.HTTPError("u", 503, "Unavailable", {}, None))
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "unknown")

    def test_a_dns_failure_is_not_a_refusal(self):
        self._patch(urllib.error.URLError("Name or service not known"))
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "unknown")

    # --- the cache key is the TRIPLE, so fixing the config re-verifies ----------

    def test_a_changed_header_re_verifies(self):
        """Keyed on the server NAME, a cached refusal would survive the operator
        fixing the very thing it recorded -- the node would stay dark after the
        repair and nobody would know why."""
        self._patch(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "refused")
        n = len(self.calls)
        self._patch(None)
        self.assertEqual(
            mcp_config.verify_server(_cfg(header="X-Admin-Token", scheme=""))[0], "ok")
        self.assertGreater(len(self.calls), n, "the endpoint was never re-asked")

    def test_a_rotated_credential_re_verifies(self):
        self._patch(urllib.error.HTTPError("u", 401, "Unauthorized", {}, None))
        mcp_config.verify_server(_cfg())
        n = len(self.calls)
        self._patch(None)
        self.assertEqual(mcp_config.verify_server(_cfg(key="nk_" + "b" * 64))[0], "ok")
        self.assertGreater(len(self.calls), n)

    def test_an_identical_config_is_asked_exactly_once(self):
        self._patch(None)
        for _ in range(5):
            mcp_config.verify_server(_cfg())
        self.assertEqual(len(self.calls), 1, "one call per (server, url, header, key)")

    # --- what the operator actually reads ---------------------------------------

    def test_the_advice_names_the_header_and_whether_it_was_declared(self):
        os.environ.pop("AI_INSTITUTE_MCP_HEADER", None)
        advice = mcp_config._header_advice(_cfg())
        self.assertIn("Authorization", advice)
        self.assertIn("DEFAULTED", advice)
        self.assertIn("X-Admin-Token", advice)
        self.assertIn("AI_INSTITUTE_MCP_HEADER", advice)

    def test_the_advice_says_declared_when_it_was(self):
        os.environ["AI_INSTITUTE_MCP_HEADER"] = "X-Admin-Token"
        try:
            advice = mcp_config._header_advice(_cfg(header="X-Admin-Token", scheme=""))
            self.assertIn("it was declared", advice)
            self.assertNotIn("DEFAULTED", advice)
        finally:
            os.environ.pop("AI_INSTITUTE_MCP_HEADER", None)

    def test_the_user_agent_is_set_so_cloudflare_does_not_invent_the_refusal(self):
        """A bare urllib UA draws 403/1010 from Cloudflare, which this function
        would then record as an auth refusal. It would invent the thing it exists
        to detect."""
        seen = {}

        def fake(req, timeout=None):
            seen.update(dict(req.header_items()))
            return _Resp(b"{}")
        urllib.request.urlopen = fake
        mcp_config.verify_server(_cfg())
        ua = next((v for k, v in seen.items() if k.lower() == "user-agent"), "")
        self.assertIn("Mozilla", ua)

    def test_the_credential_is_never_in_the_advice_string(self):
        os.environ.pop("AI_INSTITUTE_MCP_HEADER", None)
        cfg = _cfg()
        self.assertNotIn(cfg["key"], mcp_config._header_advice(cfg))


if __name__ == "__main__":
    unittest.main()
