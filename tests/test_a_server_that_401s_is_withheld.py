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
    """urlopen's context-manager surface: status, headers, read()."""
    status = 200
    headers: dict = {}

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

    def _patch(self, outcome, gated_only=False):
        """`gated_only` reproduces the institute surface: the handshake is open
        and only tools/list is gated. Measured on sg01 -- initialize returns 200
        with NO CREDENTIAL AT ALL."""
        def fake(req, timeout=None):
            self.calls.append(req.full_url)
            is_gated = b'"tools/list"' in (req.data or b"")
            if isinstance(outcome, Exception) and (is_gated or not gated_only):
                raise outcome
            return _Resp(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')
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

    def test_an_identical_config_is_verified_exactly_once(self):
        """One VERIFICATION per triple — which is two HTTP calls, because the
        probe now asks the gated `tools/list` and not only the handshake."""
        self._patch(None)
        for _ in range(5):
            mcp_config.verify_server(_cfg())
        self.assertEqual(len(self.calls), 2,
                         "expected one initialize + one tools/list, cached thereafter")

    # --- what the operator actually reads ---------------------------------------

    def test_the_advice_names_the_header_and_whether_it_was_declared(self):
        """It must NOT name a cure. LFAB-100 prescribed X-Admin-Token; rho and
        epistates both showed within the hour that the accepted header follows
        the KEY CLASS, so a fixed prescription is wrong for some other node."""
        os.environ.pop("AI_INSTITUTE_MCP_HEADER", None)
        advice = mcp_config._header_advice(_cfg())
        self.assertIn("Authorization", advice)        # what was actually sent
        self.assertIn("DEFAULTED", advice)            # where it came from
        self.assertIn("KEY CLASS", advice)            # what actually decides it
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


class TestTheProbeAsksTheGatedCall(unittest.TestCase):
    """initialize is not the entitlement, and the first version of this verifier
    asked only that. Measured on sg01 2026-09-22 against the institute surface:

        initialize   real key / zeroed key / NO CREDENTIAL   all HTTP 200
        tools/list   no credential                           HTTP 401, 791 bytes
        tools/list   a rejected credential                   HTTP 401, 1325 bytes
        tools/list   the accepted header                     HTTP 200, 57 tools

    So a verifier built on the handshake reports "ok" for a node holding nothing.
    """

    def setUp(self):
        mcp_config._CRED_VERDICT.clear()
        self._real = urllib.request.urlopen
        self.bodies = []

    def tearDown(self):
        urllib.request.urlopen = self._real
        mcp_config._CRED_VERDICT.clear()

    def test_an_open_handshake_over_a_gated_list_is_still_refused(self):
        def fake(req, timeout=None):
            self.bodies.append(req.data or b"")
            if b'"tools/list"' in (req.data or b""):
                raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
            return _Resp(b'{"jsonrpc":"2.0","id":1,"result":{}}')
        urllib.request.urlopen = fake
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "refused")
        self.assertTrue(any(b'"tools/list"' in b for b in self.bodies),
                        "the verifier never asked the gated call")

    def test_the_session_id_from_the_handshake_is_carried_to_the_gated_call(self):
        seen = {}

        class _R(_Resp):
            headers = {"Mcp-Session-Id": "sess-1"}   # noqa: RUF012

        def fake(req, timeout=None):
            if b'"tools/list"' in (req.data or b""):
                seen.update(dict(req.header_items()))
                return _Resp(b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}')
            return _R(b'{"jsonrpc":"2.0","id":1,"result":{}}')
        urllib.request.urlopen = fake
        self.assertEqual(mcp_config.verify_server(_cfg())[0], "ok")
        self.assertEqual(next((v for k, v in seen.items()
                               if k.lower() == "mcp-session-id"), None), "sess-1")


class TestADefaultedHeaderIsRepairedAndADeclaredOneIsNot(unittest.TestCase):
    """rho a3eb1148 and epistates 5435d777 both corrected LFAB-100 within the
    hour: the accepted header is a function of the KEY CLASS, so pinning one
    value globally breaks a node holding a different class. rho proposed keying
    off the key PREFIX; measured at this seat that fails too -- sg01's key is
    `nk_` and matches neither prefix the institute's own 401 documents, and
    epistates holds a third class at 49 chars matching neither.

    So the header is found by asking, and the boundary is declared-versus-default.
    """

    def setUp(self):
        mcp_config._CRED_VERDICT.clear()
        self._real = urllib.request.urlopen
        os.environ.pop("AI_INSTITUTE_MCP_HEADER", None)

    def tearDown(self):
        urllib.request.urlopen = self._real
        mcp_config._CRED_VERDICT.clear()
        os.environ.pop("AI_INSTITUTE_MCP_HEADER", None)

    def _only(self, good_header):
        def fake(req, timeout=None):
            hdrs = {k.lower(): v for k, v in req.header_items()}
            if b'"tools/list"' in (req.data or b""):
                if good_header.lower() not in hdrs:
                    raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
            return _Resp(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')
        urllib.request.urlopen = fake

    def test_a_defaulted_header_is_repaired_by_measurement(self):
        self._only("X-Admin-Token")
        got = mcp_config._resolve_live_header(_cfg())
        self.assertIsNotNone(got, "a defaulted header must be repairable")
        self.assertEqual(got["header"], "X-Admin-Token")
        self.assertEqual(got["scheme"], "")

    def test_the_other_class_is_found_too_so_the_repair_is_not_a_pinned_value(self):
        self._only("X-API-Key")
        got = mcp_config._resolve_live_header(_cfg())
        self.assertEqual(got["header"], "X-API-Key")

    def test_a_DECLARED_header_is_never_substituted(self):
        """An operator wrote it down. Silently replacing it makes the .env a
        decoration and the next debugger's first hypothesis wrong."""
        os.environ["AI_INSTITUTE_MCP_HEADER"] = "Authorization"
        self._only("X-Admin-Token")
        self.assertIsNone(mcp_config._resolve_live_header(_cfg()),
                          "a declared header must be reported, never overridden")

    def test_no_candidate_works_returns_none_rather_than_a_guess(self):
        self._only("X-Nothing-Matches-This")
        self.assertIsNone(mcp_config._resolve_live_header(_cfg()))

    def test_a_network_failure_stops_the_search_instead_of_cycling_headers(self):
        def fake(req, timeout=None):
            raise TimeoutError("timed out")
        urllib.request.urlopen = fake
        self.assertIsNone(mcp_config._resolve_live_header(_cfg()))

    def test_the_advice_no_longer_prescribes_one_header(self):
        """LFAB-100 told the room to set X-Admin-Token. Right for sg01's key,
        wrong as a rule -- and this string was carrying the wrong rule."""
        advice = mcp_config._header_advice(_cfg())
        self.assertIn("KEY CLASS", advice)
        self.assertNotIn("if that is this endpoint", advice)


if __name__ == "__main__":
    unittest.main()
