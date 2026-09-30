"""A forced collision, because probability is not a demonstration.

LFAB-113 reported that gateway_census compared a sha256[:12] seat fingerprint and
that a collision would make two credentials read as ONE seat -- permitting the
cross-seat delta report() exists to refuse -- and then declined to fix it, saying
the only remedy was to widen the prefix and rekey every stored census.

epistates 67fb6308-79c7-4671-b934-957098c950a1 did two things that packet did not:

  1. FORCED a collision and showed the diff proceeded, rather than reasoning from
     a birthday probability. "2.39e-03" is a claim about a population; a forced
     collision is a claim about the code.
  2. Fixed it by a SPLIT rather than a widening -- publish the short fingerprint,
     compare the full digest -- which costs no baseline at all.

Both adopted. These cases force the collision.
"""
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "_gc", os.path.join(_ROOT, "scripts", "gateway_census.py"))
_gc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gc)


def _cap(digest, fingerprint, tools, seat="sg01", host="gw.example"):
    return {"measured_at": "2026-09-23T00:00:00+00:00", "seat": seat,
            "endpoint_host": host, "key_fingerprint": fingerprint,
            "key_digest": digest, "total": len(tools),
            "namespaces": {"(bare)": len(tools)},
            "tools": {t: [] for t in tools}}


def _report(now, prev):
    buf = io.StringIO()
    with redirect_stdout(buf):
        _gc.report(now, prev)
    return buf.getvalue()


class TestTheSeatKeyIsNotLossy(unittest.TestCase):

    def test_a_FORCED_fingerprint_collision_is_still_refused(self):
        """The demonstration LFAB-113 owed. Two DIFFERENT credentials whose short
        fingerprints collide must not compare as one seat."""
        prev = _cap("d" * 64, "aaaaaaaaaaaa", ["a", "b", "c"])
        now = _cap("e" * 64, "aaaaaaaaaaaa", ["a"])       # same prefix, different key
        out = _report(now, prev)
        self.assertIn("DELTA REFUSED", out)
        self.assertNotIn("removed:", out)

    def test_the_same_credential_still_produces_a_delta(self):
        """The control: if the fix refused everything it would be useless."""
        prev = _cap("d" * 64, "aaaaaaaaaaaa", ["a", "b", "c"])
        now = _cap("d" * 64, "aaaaaaaaaaaa", ["a", "b"])
        out = _report(now, prev)
        self.assertNotIn("DELTA REFUSED", out)
        self.assertIn("removed:", out)
        self.assertIn("c", out)

    def test_a_genuinely_different_seat_is_refused(self):
        prev = _cap("d" * 64, "aaaaaaaaaaaa", ["a", "b"])
        now = _cap("e" * 64, "bbbbbbbbbbbb", ["a", "b"])
        self.assertIn("DELTA REFUSED", _report(now, prev))

    # --- the legacy capture, which must not silently take the weak path --------

    def test_a_capture_without_a_digest_compares_as_PREFIX_ONLY(self):
        old = _cap(None, "aaaaaaaaaaaa", ["a"])
        del old["key_digest"]
        self.assertEqual(_gc._seat_key(old), "PREFIX-ONLY:aaaaaaaaaaaa")

    def test_a_digested_capture_never_equals_a_prefix_only_one(self):
        """The failure this prevents: a new capture silently comparing equal to an
        old one on twelve characters."""
        old = _cap(None, "aaaaaaaaaaaa", ["a"])
        del old["key_digest"]
        new = _cap("d" * 64, "aaaaaaaaaaaa", ["a"])
        self.assertNotEqual(_gc._seat_key(old), _gc._seat_key(new))

    def test_no_credential_at_all_is_None_not_a_prefix_token(self):
        bare = _cap(None, None, ["a"])
        del bare["key_digest"]
        self.assertIsNone(_gc._seat_key(bare))

    # --- and the published value is still the short one -----------------------

    def test_the_full_digest_is_never_printed(self):
        prev = _cap("d" * 64, "aaaaaaaaaaaa", ["a", "b"])
        now = _cap("d" * 64, "aaaaaaaaaaaa", ["a"])
        out = _report(now, prev) + _report(now, None)
        self.assertNotIn("d" * 64, out, "the compared digest must not reach stdout")

    def test_display_and_comparison_do_not_share_a_path(self):
        h = {"Authorization": "Bearer secret-value"}
        self.assertNotEqual(_gc._key_digest(h), _gc._key_fingerprint(h))
        self.assertTrue(_gc._key_digest(h).startswith(_gc._key_fingerprint(h)))
        self.assertEqual(len(_gc._key_fingerprint(h)), 12)
        self.assertEqual(len(_gc._key_digest(h)), 64)


class TestTheCorrespondenceSeparatesItsConfidenceClasses(unittest.TestCase):
    """The SECOND lossy comparison key in this file, found by rho's triage rule.

    LFAB-113 audited by grepping for slice syntax. `_norm(n) = n.rsplit("__",1)[-1]`
    is not a slice and the grep missed it -- the audit's own pattern was a
    vocabulary, which is the defect the audit was looking for.

    Forced, as rho dab93731 and epistates 67fb6308 both insist: ONE candidate on
    each side, different namespaces --

        prev seat  alpha__report        now seat  beta__report
        BEFORE     "matched  1", no flag. Different tools, paired confidently.

    The matching is not the defect: seats namespace differently and suffix-matching
    is the whole point. Two confidence classes sharing one line was.
    """

    def _cross_seat(self, prev_tools, now_tools):
        return _report(_cap("e" * 64, "bbbbbbbbbbbb", now_tools, seat="B"),
                       _cap("d" * 64, "aaaaaaaaaaaa", prev_tools, seat="A"))

    def test_a_cross_namespace_pair_is_flagged_as_resting_on_the_basis(self):
        out = self._cross_seat(["alpha__report"], ["beta__report"])
        self.assertIn("1 where the namespaces DIFFER", out)
        self.assertIn("rest ENTIRELY on the basis", out)
        self.assertIn("alpha__report  <->  beta__report", out)

    def test_a_same_namespace_pair_is_NOT_flagged(self):
        """The control. A guard that flagged every match would be useless."""
        out = self._cross_seat(["inst__report"], ["inst__report"])
        self.assertIn("1 with the SAME namespace", out)
        self.assertNotIn("rest ENTIRELY on the basis", out)

    def test_the_total_still_counts_both_classes(self):
        out = self._cross_seat(["inst__a", "alpha__b"], ["inst__a", "beta__b"])
        self.assertIn("matched      2", out)
        self.assertIn("(1 with the SAME namespace, 1 where the namespaces DIFFER)", out)

    def test_an_ambiguous_suffix_still_fails_CLOSED_naming_both(self):
        """This arm already failed closed before the change and must keep doing so."""
        out = self._cross_seat(["alpha__report"], ["beta__report", "gamma__report"])
        self.assertIn("AMBIGUOUS", out)
        self.assertIn("beta__report, gamma__report", out)


if __name__ == "__main__":
    unittest.main()
