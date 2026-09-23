"""A failure saved where DATA belongs used to score CLEAN in the sweep.

scripts/sweep_refusals.py has printed, at the foot of every run since it was
written, that a large saved error PAGE is "the same class of defect and is
outside this detector by construction." That is a scope line, and this estate
has now learned three times that a scope line is a map of where to look.

Measured 2026-09-23 across both nodes' workspaces, over the 2,387 `.json` files
that do not parse:

    271   an upstream failure saved to a `.json` path
   1353   YAML/markdown front-matter under a `.json` name — NOT a failure
    554   truncated or malformed JSON
    108   binary blobs
     84   empty

THE PART THAT MAKES IT A DEFECT RATHER THAN A GAP: every one of the 271 was
INSIDE the sweep's eligible population. `.json` is in EXT, they are far under
the 4 KiB cap, and refusal_envelope returns None because they are not JSON — so
they were counted as EXAMINED AND PASSED. Not uncounted. Counted clean.

The detector is keyed on the OPENING and its vocabulary is declared at module
scope and printed by the sweep, because a list a reader can see can be argued
with and this estate has shipped eleven hidden ones.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.institute_files import (saved_failure, refusal_envelope,   # noqa: E402
                                 SAVED_FAILURE_OPENINGS)


class TestSavedFailure(unittest.TestCase):

    # ---- the real bodies, verbatim from the nodes ---------------------------
    def test_the_bodies_actually_found_on_the_nodes(self):
        for body in ("404 Not Found",                                    # 13 B
                     "Edge: Too Many Requests",                          # 23 B
                     "error code: 1015",                                 # 17 B
                     "Client sent an HTTP request to an HTTPS server. ",  # 48 B
                     "HTTP/2 200 \r\ndate: Tue, 02 Jun 2026 09:10:50 GMT"):
            self.assertIsNotNone(saved_failure(body), body[:30])

    def test_markup_error_pages(self):
        for body in ("<html>\r\n<head><title>503 Service Temporarily "
                     "Unavailable</title></head>",
                     "<HTML><HEAD> <TITLE>Access Denied</TITLE> </HEAD>",
                     '<?xml version="1.0" encoding="UTF-8"?>'
                     "<Error><Code>AccessDenied</Code></Error>"):
            self.assertIsNotNone(saved_failure(body), body[:40])

    # ---- the negatives, which are what stop this being a keyword search -----
    def test_markup_that_is_not_an_error_page_does_not_fire(self):
        self.assertIsNone(saved_failure(
            "<html><head><title>Quarterly figures</title></head>"
            "<body><table><tr><td>1</td></tr></table></body></html>"))

    def test_a_document_that_MENTIONS_a_failure_does_not_fire(self):
        """The whole distinction rho 84468a1d-e61b-4ca3-9a03-e3da6d221a0a §2
        drew on their 401 blobs: 0 led with the failure, 401 merely mentioned
        it. Keyed on the opening, so a report about a 404 is not a saved 404."""
        self.assertIsNone(saved_failure(
            "Our Q3 review notes a 404 Not Found on the vendor endpoint, "
            "which the team resolved by rotating the key."))

    def test_legitimate_data_does_not_fire(self):
        for body in ('{"rows": [1, 2, 3]}',
                     "---\n# OKF-report\ntitle: packaging\n",
                     "date,open,close\n2026-01-01,1,2\n",
                     ""):
            self.assertIsNone(saved_failure(body), repr(body[:24]))

    # ---- the two detectors answer different questions ----------------------
    def test_the_two_detectors_do_not_overlap_on_their_own_cases(self):
        envelope = '{"error": "Invalid API key"}'
        plain = "Edge: Too Many Requests"
        self.assertIsNotNone(refusal_envelope(envelope))
        self.assertIsNone(saved_failure(envelope),
                          "a JSON envelope belongs to refusal_envelope")
        self.assertIsNotNone(saved_failure(plain))
        self.assertIsNone(refusal_envelope(plain),
                          "a non-JSON body is invisible to refusal_envelope — "
                          "which is exactly why these scored clean")

    def test_the_vocabulary_is_declared_at_module_scope(self):
        """Not 'think of more words' — 'move the list where a reader sees it'."""
        self.assertIn("404 Not Found", SAVED_FAILURE_OPENINGS)
        self.assertGreater(len(SAVED_FAILURE_OPENINGS), 8)

    # ---- and the sweep must no longer call them clean -----------------------
    def test_the_sweep_reports_them_separately_from_hits(self):
        from scripts.sweep_refusals import sweep
        d = tempfile.mkdtemp(prefix="sweep-savedfail-")
        for name, body in (("a.json", "404 Not Found"),
                           ("b.json", "Edge: Too Many Requests"),
                           ("ok.json", '{"rows": [1, 2, 3]}'),
                           ("env.json", '{"error": "Invalid API key"}')):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(body)
        walked, eligible, hits, unreadable, skipped, saved = sweep(d)
        self.assertEqual(eligible, 4)
        self.assertEqual(len(hits), 1, "the JSON envelope is still a hit")
        self.assertEqual(len(saved), 2, "and the two non-JSON failures are NOT clean")
        self.assertEqual(sorted(os.path.basename(x[0]) for x in saved),
                         ["a.json", "b.json"])


if __name__ == "__main__":
    unittest.main()
