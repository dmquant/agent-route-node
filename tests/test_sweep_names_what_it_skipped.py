"""The extension filter dropped rows into no denominator, and this file had
already fixed that once for a different exclusion.

`ec35293` made an UNREADABLE file visible, because rho's backwards walk showed the
published 1,304 could not see one. The extension filter three lines above it was
left alone -- the remedy attached to a PATH while the population was every
exclusion, which is rho's own recurring observation, in this file, twice.

FORCED 2026-09-23, one identical refusal envelope under 18 real-world extensions:

    18 written · 9 found · 9 dropped
    `walked` read 9, so the nine were in NO denominator
    missing: .jsonl .ndjson .yml.bak .out .result .response .err .tmp .data

agora-pm b4ce12a4-2844-45f2-8ac3-8644d59b6f0a predicted the direction before it
was measured: a lossy predicate collides toward the permissive branch, and here
that branch is "not our kind of file, skip". `.err`, `.out`, `.result` and
`.response` are what a saved refusal is actually called.

NOT fixed by widening EXT -- a longer list is still a list of what its author
thought of, and this estate has shipped that eight times tonight.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_spec = importlib.util.spec_from_file_location(
    "_sw", os.path.join(_ROOT, "scripts", "sweep_refusals.py"))
_sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sw)

ENVELOPE = json.dumps({"error": "Unauthorized", "credential_presented": True})
ADMITTED = ["a.json", "b.md", "c.txt", "d.log", "e.yaml", "f.csv", "g.html", "noext"]
SKIPPED = ["h.jsonl", "i.ndjson", "j.yml.bak", "l.out", "m.result",
           "n.response", "o.err", "p.tmp", "q.data"]


def _tree(names):
    d = tempfile.mkdtemp(prefix="sweepext_")
    for n in names:
        with open(os.path.join(d, n), "w") as fh:
            fh.write(ENVELOPE)
    return d


class TestTheSweepNamesWhatItSkipped(unittest.TestCase):

    def test_the_skipped_files_are_counted_and_their_extensions_named(self):
        walked, eligible, hits, unreadable, skipped, _saved = _sw.sweep(_tree(SKIPPED))
        self.assertEqual(hits, [], "none of these are admitted, which is the point")
        self.assertEqual(sum(skipped.values()), len(SKIPPED))
        for ext in (".jsonl", ".ndjson", ".out", ".result", ".response", ".err"):
            self.assertIn(ext, skipped, "%s must be named, not silently dropped" % ext)

    def test_an_identical_refusal_is_found_under_an_admitted_extension(self):
        """The control: if the detector missed these too, the skip count would be
        measuring nothing."""
        walked, eligible, hits, unreadable, skipped, _saved = _sw.sweep(_tree(ADMITTED))
        self.assertEqual(len(hits), len(ADMITTED))
        self.assertEqual(skipped, {})

    def test_the_skipped_are_in_NO_denominator_which_is_why_they_must_be_reported(self):
        """walked and eligible both exclude them -- the fact that made the silence
        dangerous rather than merely untidy."""
        walked, eligible, hits, unreadable, skipped, _saved = _sw.sweep(_tree(SKIPPED))
        self.assertEqual(walked, 0)
        self.assertEqual(eligible, 0)
        self.assertGreater(sum(skipped.values()), 0,
                           "a population outside every denominator must still be counted")

    def test_a_mixed_tree_reports_both_populations(self):
        walked, eligible, hits, unreadable, skipped, _saved = _sw.sweep(_tree(ADMITTED + SKIPPED))
        self.assertEqual(len(hits), len(ADMITTED))
        self.assertEqual(sum(skipped.values()), len(SKIPPED))

    def test_the_fix_is_not_a_wider_vocabulary(self):
        """EXT must not have grown. Widening the list is the defect this estate has
        shipped eight times; the fix is to count what the list excludes."""
        for ext in (".jsonl", ".ndjson", ".out", ".err", ".result", ".response"):
            self.assertNotIn(ext, _sw.EXT,
                             "%s was ADDED to EXT -- that is the vocabulary trap, "
                             "not the repair" % ext)


if __name__ == "__main__":
    unittest.main()
