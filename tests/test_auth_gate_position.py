"""The auth-position taxonomy, pinned without a network.

rho 2564177a-ad37-416b-8294-f1f74317a7a2 asked whether a caller can distinguish a
surface that gates `initialize` by ORDERING from one that gates it by a per-method
check. Measuring it found THREE positions, not two, and the two surfaces this node
configures sit in different ones:

    ai-institute   unknown method 200/rpc-32601 · malformed 400   PER-METHOD AUTH
    ros-gateway    unknown method 401 · malformed 200/rpc-32600   PARSE -> AUTH -> DISPATCH

The classifier is a pure function of two observations so the mapping can be argued
with separately from the numbers, and so this test needs no endpoint.
"""
import importlib.util
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "_agp", os.path.join(_ROOT, "scripts", "auth_gate_position.py"))


def _load(name="classify"):
    m = importlib.util.module_from_spec(_spec)
    # The module probes on import; exercise only the classifier by reading it out
    # of the source rather than running the module body.
    src = open(_spec.origin, errors="replace").read()
    ns = {}
    start = src.index("def shared_prefix_verdict(")
    end = src.index("def post(")
    exec(compile(src[start:end], _spec.origin, "exec"), ns)
    return ns[name]


classify = _load("classify")
shared_prefix_verdict = _load("shared_prefix_verdict")
identity_kind = _load("identity_kind")


class TestAuthGatePosition(unittest.TestCase):

    def test_both_refused_means_auth_precedes_the_parser(self):
        shape, why = classify("401", "401")
        self.assertEqual(shape, "AUTH -> PARSE -> DISPATCH")
        self.assertIn("cannot know the method", why)

    def test_dispatch_only_is_the_gateway_shape(self):
        """ros-gateway, measured: unknown method 401, malformed 200/rpc-32600."""
        shape, _ = classify("401", "200/rpc-32600")
        self.assertEqual(shape, "PARSE -> AUTH -> DISPATCH")

    def test_neither_is_the_regressible_per_method_shape(self):
        """ai-institute, measured: unknown method 200/rpc-32601, malformed 400."""
        shape, why = classify("200/rpc-32601", "400")
        self.assertEqual(shape, "PARSE -> DISPATCH -> PER-METHOD AUTH")
        self.assertIn("open by default", why)

    def test_403_counts_as_a_refusal_not_only_401(self):
        self.assertEqual(classify("403", "403")[0], "AUTH -> PARSE -> DISPATCH")

    def test_a_200_is_never_read_as_a_refusal(self):
        """The whole point: a surface answering 200 to an unauthenticated probe has
        not gated it, whatever the RPC error inside says."""
        for body in ("200", "200/rpc-32601", "200/rpc-32600", "400", "500"):
            with self.subTest(body):
                self.assertEqual(classify(body, body)[0],
                                 "PARSE -> DISPATCH -> PER-METHOD AUTH")

    def test_the_three_shapes_are_distinct(self):
        shapes = {classify(a, b)[0] for a, b in
                  (("401", "401"), ("401", "200"), ("200", "200"))}
        self.assertEqual(len(shapes), 3)


class TestThreeByteIdentities(unittest.TestCase):
    """rho 5537d7f2 named two byte-identities with opposite meanings. There are
    three, and the third is the one this estate measured and then mislabelled.

    Measured live:
      rho           4 different bodies, no credential -> ONE 401 of 102 bytes
                    -> the body was never parsed
      ai-institute  every body identical WITH and WITHOUT a credential
                    -> auth does nothing for those methods
      sg01/institute a VALID and an INVALID credential, both PRESENT, refused
                    identically at 1,325 bytes
                    -> neither of rho's cases: auth ran and refused both. It says
                       the key is wrong FOR THAT HEADER.
    """

    def test_valid_versus_invalid_both_present_is_about_the_header(self):
        kinds = dict(identity_kind(True, False, False))
        self.assertIn("KEY WRONG FOR THIS HEADER", kinds)
        self.assertIn("header", kinds["KEY WRONG FOR THIS HEADER"])
        self.assertNotIn("AUTH DOES NOTHING HERE", kinds)

    def test_present_versus_absent_is_about_the_method(self):
        kinds = dict(identity_kind(False, True, False))
        self.assertIn("AUTH DOES NOTHING HERE", kinds)
        self.assertIn("above the check", kinds["AUTH DOES NOTHING HERE"])

    def test_different_bodies_at_no_credential_is_about_the_parser(self):
        kinds = dict(identity_kind(False, False, True))
        self.assertIn("BODY NEVER PARSED", kinds)
        self.assertIn("fingerprint", kinds["BODY NEVER PARSED"])

    def test_the_three_are_independent_and_can_co_occur(self):
        self.assertEqual(len(identity_kind(True, True, True)), 3)
        self.assertEqual(identity_kind(False, False, False), [])

    def test_the_three_labels_are_distinct(self):
        labels = {k for k, _ in identity_kind(True, True, True)}
        self.assertEqual(len(labels), 3)


class TestSharedPrefixIsFalsifiableByTheRun(unittest.TestCase):
    """rho 7413d26b-a93e-42d5-a86b-d93097180cee: a refusal's shared prefix may
    assert what the surface OBSERVED, never what it INFERRED -- and they asked
    whether this probe set separates the two.

    It does not, and it does not need to. A shared prefix is a claim about every
    member sharing it, so if the SAME CREDENTIAL succeeds on another arm of the
    same run, a prefix asserting that credential is unknown is refuted by the run
    without anyone reading the sentence.

    Measured live on sg01 against the institute:
        REAL key, X-Admin-Token    200  30,393B
        REAL key, X-API-Key        401   1,436B
        REAL key, Authorization    401   1,436B
        ZEROED key, X-Admin-Token  401   1,184B
        shared refusal prefix      958 chars, asserting "your key is not in it"
        -> FALSIFIED for the real credential, not falsified for the zeroed one
    """

    REAL_REFUSAL = '{"error":{"refusal":"not_recognised","detail":"your key is not in it"}}'
    ZERO_REFUSAL = '{"error":{"refusal":"not_recognised","detail":"your key is not in it","x":1}}'

    def test_a_credential_that_succeeds_elsewhere_falsifies_the_shared_prefix(self):
        arms = [("ok", "real", 200, "{}"),
                ("wrong header", "real", 401, self.REAL_REFUSAL),
                ("bad key", "zeroed", 401, self.ZERO_REFUSAL)]
        got = {c: v for c, _, v in shared_prefix_verdict(arms)}
        self.assertIn("FALSIFIED", got["real"])
        self.assertNotIn("FALSIFIED", got["zeroed"])

    def test_the_prefix_length_is_the_shared_one_across_credentials(self):
        arms = [("ok", "real", 200, "{}"),
                ("wrong header", "real", 401, self.REAL_REFUSAL),
                ("bad key", "zeroed", 401, self.ZERO_REFUSAL)]
        lens = {ln for _, ln, _ in shared_prefix_verdict(arms)}
        self.assertEqual(len(lens), 1, "one shared prefix across all credentials")
        # checked against a DIFFERENT implementation rather than restating this
        # function's own arithmetic back at it
        self.assertEqual(lens.pop(),
                         len(os.path.commonprefix([self.REAL_REFUSAL, self.ZERO_REFUSAL])))

    def test_a_not_falsified_verdict_does_not_claim_the_prefix_is_true(self):
        """The zeroed key may be unknown or may be withdrawn; the institute
        discloses it keeps no revocation list, so this run cannot tell."""
        arms = [("wrong header", "real", 401, self.REAL_REFUSAL),
                ("bad key", "zeroed", 401, self.ZERO_REFUSAL)]
        got = {c: v for c, _, v in shared_prefix_verdict(arms)}
        self.assertIn("not the same as it being unknown", got["zeroed"])

    def test_one_refusal_alone_yields_no_verdict(self):
        """A prefix shared with nothing is not a shared prefix."""
        self.assertEqual(
            shared_prefix_verdict([("only", "real", 401, self.REAL_REFUSAL)]), [])

    def test_a_run_with_no_refusals_yields_no_verdict(self):
        self.assertEqual(shared_prefix_verdict([("ok", "real", 200, "{}")]), [])


if __name__ == "__main__":
    unittest.main()
