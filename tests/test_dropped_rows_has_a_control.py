"""The row-dropping scan must be refuted by cases its author planted against it.

rho 445121ed-3953-4863-b068-288120ae29ca found `except ...: continue` with a regex
that only reaches a one-line bare handler, and asked whether the row-dropping class
has a structural form at all or needs the backwards walk from a published figure
every time. It has one, and this pins it: the two forms their regex missed must
FIRE, and every honest disposition of a failure must stay SILENT.

rho 3ca0ef27 is why the negatives outnumber the positives here. A MECHANISM's
correctness is fully specified by its author, so plants are complete; a DETECTOR's
coverage is not, and plants only ever bound its OVER-firing. The first run of this
scan reported 41 sites and most were handlers that fall through to the accumulator
on the next line -- a shape count presented as a finding. Every negative below was
a live false positive before it was a test.
"""
import ast
import importlib.util
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "_avu", os.path.join(_ROOT, "scripts", "absent_vs_unreadable.py"))
_avu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_avu)

# The two forms rho's regex could not reach, plus the bare form it could.
FIRES = {
    "bare continue":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception:\n        continue\n",
    "logs THEN continues":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception as e:\n        log(e)\n        continue\n",
    "appends to a list nobody reads":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception as e:\n        errs.append(e)\n        continue\n",
    "nested loops -- still ONE site":
        "for d in ds:\n    for f in fs:\n        n += 1\n        try:\n"
        "            r(f)\n        except Exception:\n            continue\n",
}

# Every one of these was a false positive on a real run of this estate.
SILENT = {
    # sweep_refusals: `except OSError: nbytes = -1` then hits.append(...) below.
    "falls through to the accumulator":
        "for f in fs:\n    n += 1\n    try:\n        b = sz(f)\n"
        "    except OSError:\n        b = -1\n    hits.append(b)\n",
    # task_puller: `except CancelledError: return`. The accumulator is abandoned,
    # not published short -- the failure goes UP instead of being absorbed.
    "returns the failure to the caller":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception as e:\n        return [], str(e)\n",
    "re-raises":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception:\n        raise\n",
    "appends to a list that IS read":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception as e:\n        errs.append(e)\n        continue\nprint(errs)\n",
    # The repair this scan drove in retraction_audit, which it then flagged because
    # its accumulate vocabulary was append/add/extend/update and a dict uses neither.
    "records it in a dict by setdefault":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception as e:\n        bad.setdefault(f, e)\n        continue\nprint(bad)\n",
    "records it in a dict by subscript":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception as e:\n        bad[f] = e\n        continue\nprint(bad)\n",
    "counts the failure":
        "for f in fs:\n    n += 1\n    try:\n        r(f)\n"
        "    except Exception:\n        bad += 1\n        continue\nprint(bad)\n",
    "the loop accumulates nothing at all":
        "for f in fs:\n    try:\n        r(f)\n    except Exception:\n        continue\n",
    "ABSENT-OK opt-out above the try":
        "for f in fs:\n    n += 1\n    # ABSENT-OK: a vanished temp file is not a row\n"
        "    try:\n        r(f)\n    except OSError:\n        continue\n",
}


class TestDroppedRowScanHasAControl(unittest.TestCase):

    def test_the_forms_rho_s_regex_missed_all_fire(self):
        for label, src in FIRES.items():
            with self.subTest(label):
                hits = _avu.scan_dropped_rows(src, "<planted>")
                self.assertEqual(len(hits), 1, "%s -> %r" % (label, hits))

    def test_every_honest_disposition_stays_silent(self):
        for label, src in SILENT.items():
            with self.subTest(label):
                self.assertEqual(
                    _avu.scan_dropped_rows(src, "<planted>"), [],
                    "%s must not be reported -- it was a live false positive" % label)

    def test_a_site_is_counted_once_per_handler_not_once_per_enclosing_loop(self):
        """`continue` binds to the INNERMOST loop, so nesting must not double-count.

        The first estate run emitted 13 rows holding 9 distinct lines, because
        ast.walk visits both the outer loop and the inner one and the handler sits
        inside both. A detector that double-counts its own hits is the same
        arithmetic error as a denominator that drops rows, one level up.
        """
        src = ("for a in xs:\n    for b in ys:\n        for c in zs:\n"
               "            n += 1\n            try:\n                r(c)\n"
               "            except Exception:\n                continue\n")
        hits = _avu.scan_dropped_rows(src, "<planted>")
        self.assertEqual(len(hits), 1, hits)

    def test_the_scan_itself_runs_over_this_repo_without_raising(self):
        """A scan that cannot parse the estate reports a clean zero, which is the
        defect. Parse every file and require the scan to survive all of them."""
        seen = 0
        for root in ("app", "scripts", "tests"):
            for dp, dirs, files in os.walk(os.path.join(_ROOT, root)):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    if not f.endswith(".py"):
                        continue
                    p = os.path.join(dp, f)
                    with open(p, errors="replace") as fh:
                        src = fh.read()
                    ast.parse(src)          # if this raises, the scan is blind here
                    _avu.scan_dropped_rows(src, p)
                    seen += 1
        self.assertGreater(seen, 30, "scanned %d files -- too few to mean anything" % seen)

    def test_the_repair_to_sweep_refusals_is_visible_to_this_scan(self):
        """Before/after on the site rho's backwards walk actually found.

        sweep_refusals fed the published 1,304. It held `except Exception: continue`
        inside the walk loop, so an unreadable file was absent from `walked`, from
        `eligible` and from the hits alike. The repair names it and counts it, and
        this asserts the scan can SEE that difference -- otherwise the before/after
        that justifies the repair is a story rather than a measurement.
        """
        with open(os.path.join(_ROOT, "scripts", "sweep_refusals.py"),
                  errors="replace") as fh:
            after = fh.read()
        self.assertEqual(_avu.scan_dropped_rows(after, "sweep_refusals.py"), [])
        before = after.replace(
            "            except Exception as e:\n"
            "                unreadable.append((p, type(e).__name__))\n"
            "                continue\n",
            "            except Exception:\n                continue\n")
        self.assertNotEqual(before, after, "the repair's text moved; update this control")
        self.assertEqual(len(_avu.scan_dropped_rows(before, "sweep_refusals.py")), 1)


if __name__ == "__main__":
    unittest.main()
