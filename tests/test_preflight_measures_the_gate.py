"""`proves` was a CONSTANT STRING, and constants do not measure anything.

preflight() published, to every caller, the sentence "reachability only —
tools/list needs no auth here". The same file's _probe had already recorded the
measurement that overturns it, and today's three-arm run settles it from two
nodes against both endpoints:

    ai-institute   real key 200/57 · zeroed 401 · absent 401
    ros-gateway    real key 200/92 · zeroed 401 · absent 401

tools/list is GATED on both, so a 200 there proves the credential was accepted.

The direction of the old error is why this test exists rather than a one-word
edit. The constant UNDERSTATED what a 200 proved, so a caller needing the
stronger fact had to write a second probe by hand -- three were written by hand
on 2026-09-23 and two repeated the exact `initialize`-is-not-gated defect that
137e772 had already fixed inside this module. A stale caveat manufactures the
duplicate instrument that carries the next defect.

So the property under test is not "the string is right today". It is that TWO
DIFFERENT WORLDS PRODUCE TWO DIFFERENT `proves` STRINGS -- a caveat that cannot
change when the endpoint changes is a constant wearing a measurement's clothes.
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
    status = 200
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cfg(name="ai-institute"):
    return {"name": name, "url": "https://example.invalid/api/mcp",
            "key": "nk_" + "a" * 64, "header": "X-Admin-Token", "scheme": "",
            "key_var": "AI_INSTITUTE_MCP_KEY", "url_var": "AI_INSTITUTE_MCP_URL",
            "hands": ["claude"]}


class TestPreflightMeasuresTheGate(unittest.TestCase):

    def setUp(self):
        self._real_urlopen = urllib.request.urlopen
        self._real_servers = mcp_config.servers_for
        self.seen = []

    def tearDown(self):
        urllib.request.urlopen = self._real_urlopen
        mcp_config.servers_for = self._real_servers

    def _install(self, servers, answer):
        """answer(has_credential) -> ("ok", status) | ("http", code) | ("boom", exc)"""
        mcp_config.servers_for = lambda hand: servers

        def fake(req, timeout=None):
            has = any(k.lower() in ("x-admin-token", "authorization", "x-api-key")
                      for k in req.headers)
            self.seen.append(has)
            kind, v = answer(has)
            if kind == "ok":
                r = _Resp(b'{"result":{"tools":[]}}')
                r.status = v
                return r
            if kind == "http":
                raise urllib.error.HTTPError(req.full_url, v, "refused", {},
                                             io.BytesIO(b"{}"))
            raise v
        urllib.request.urlopen = fake

    # ---- the arm that matters: a gated endpoint ------------------------------
    def test_a_refused_control_means_the_200_proved_the_credential(self):
        self._install([_cfg()], lambda has: ("ok", 200) if has else ("http", 401))
        out = mcp_config.preflight("claude")
        self.assertEqual(out["reachable"], ["ai-institute"])
        self.assertIs(out["gated"]["ai-institute"], True)
        self.assertIn("credential acceptance", out["proves"])
        # The control ran: two requests, one with and one without.
        self.assertEqual(sorted(self.seen), [False, True])

    # ---- the arm the old constant asserted ----------------------------------
    def test_an_open_endpoint_is_reported_as_reachability_only(self):
        self._install([_cfg()], lambda has: ("ok", 200))
        out = mcp_config.preflight("claude")
        self.assertIs(out["gated"]["ai-institute"], False)
        self.assertIn("reachability only", out["proves"])

    # ---- an unmeasured gate is NOT an open one ------------------------------
    def test_a_control_that_could_not_run_is_None_and_says_so(self):
        self._install([_cfg()],
                      lambda has: ("ok", 200) if has else ("boom", TimeoutError("no route")))
        out = mcp_config.preflight("claude")
        self.assertIsNone(out["gated"]["ai-institute"])
        self.assertIn("UNMEASURED", out["proves"])
        # and it must not be mistaken for the open-endpoint wording
        self.assertNotIn("reachability only on at least one server", out["proves"])

    # ---- THE REGRESSION GUARD, and the reason this file exists --------------
    def test_proves_is_not_a_constant(self):
        """Two worlds, two strings. A caveat that cannot change is not a finding."""
        self._install([_cfg()], lambda has: ("ok", 200) if has else ("http", 401))
        gated = mcp_config.preflight("claude")["proves"]
        self.tearDown(); self.setUp()
        self._install([_cfg()], lambda has: ("ok", 200))
        open_ = mcp_config.preflight("claude")["proves"]
        self.assertNotEqual(gated, open_)
        # and neither may carry the superseded sentence
        for s in (gated, open_):
            self.assertNotIn("tools/list needs no auth", s)

    # ---- an empty population must not borrow a population's verdict ---------
    def test_no_configured_server_proves_nothing(self):
        self._install([], lambda has: ("ok", 200))
        out = mcp_config.preflight("claude")
        self.assertEqual(out["checked"], [])
        self.assertIn("nothing", out["proves"])
        self.assertEqual(self.seen, [])

    # ---- a refusal WITH the credential is unreachable, not merely ungated ----
    def test_a_server_that_401s_with_the_credential_is_unreachable(self):
        self._install([_cfg()], lambda has: ("http", 401))
        out = mcp_config.preflight("claude")
        self.assertEqual(out["reachable"], [])
        self.assertEqual(out["unreachable"][0]["name"], "ai-institute")
        self.assertIn("401", out["unreachable"][0]["why"])
        # It is gated AND it refuses us -- both facts, neither hiding the other.
        self.assertIs(out["gated"]["ai-institute"], True)


if __name__ == "__main__":
    unittest.main()
