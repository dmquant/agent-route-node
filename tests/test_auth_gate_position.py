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


def _load():
    m = importlib.util.module_from_spec(_spec)
    # The module probes on import; exercise only the classifier by reading it out
    # of the source rather than running the module body.
    src = open(_spec.origin, errors="replace").read()
    ns = {}
    start = src.index("def classify(")
    end = src.index("def post(")
    exec(compile(src[start:end], _spec.origin, "exec"), ns)
    return ns["classify"]


classify = _load()


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


if __name__ == "__main__":
    unittest.main()
