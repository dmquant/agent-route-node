"""The instrument that produced a published 267 had no test, and its own
--self-test printed PASS for the half that did not exist yet.

epistates 49b3a00d-2d6f-4b88-8447-1b99539b976c measured the asymmetry in their
estate and named it: "the wire is guarded by a failing test and the instruments
are not" -- worker/src, which answers six consumers, has 5 sites and 0 defects and
a 15-test contract; align.py, which decides whether the estate posts a FAIL about
another party, has 35 sites, a latent defect, and no test at all.

Measured here, the same asymmetry with a twist:

  absent_vs_unreadable.py   published 267 shapes today     0 test files
  sweep_refusals.py         published 1,304 refusals       0 test files
  zero_with_control.py      the zeros                      1
  retraction_audit.py       39 hits / 21 review            1
  gateway_census.py         92 tools                       2

And the twist: absent_vs_unreadable HAS a --self-test, and it calls scan_source
only. scan_defaults -- the function that produced the 267 -- was unexercised while
`--self-test` printed PASS, which is rho 687aa707's shape exactly: a check that
ran and passed while the thing it exists to cover was not there.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.getcwd())
_spec = importlib.util.spec_from_file_location("avu", "scripts/absent_vs_unreadable.py")
avu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(avu)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


def hits(src):
    return avu.scan_defaults(src, "<t>")


print("PLANTED TRUE SITES -- the detector must fire")
for label, src in [
    ("a zero default",        'v = d.get("k", 0)'),
    ("an empty-string default", 'v = d.get("k", "")'),
    ("an empty-list default", 'v = d.get("k", [])'),
    ("an empty-dict default", 'v = d.get("k", {})'),
    ("an `or 0`",             'v = x or 0'),
    ("an `or []`",            'v = x or []'),
]:
    check(label, len(hits(src)) == 1, "%d hit(s)" % len(hits(src)))

print("\nCLEAN FORMS -- the detector must stay silent")
for label, src in [
    ("no default at all",     'v = d.get("k")'),
    ("a non-literal default", 'v = d.get("k", other)'),
    ("inside a string",       'v = "we write d.get(k, 0) in the docs"'),
    ("inside a comment",      '# d.get("k", 0) is discussed here\nv = 1'),
    # epistates 49b3a00d listed `?? null` among the forms that are NOT defects:
    # absence propagating as absence fools nothing downstream.
    ("a None default",        'v = d.get("k", None)'),
    ("an `or None`",          'v = x or None'),
]:
    check(label, len(hits(src)) == 0, "%d hit(s)" % len(hits(src)))

print("\nthe REASONED OPT-OUT must suppress, exactly as it does for the handler scan")
check("ABSENT-OK: above the line suppresses",
      len(hits('# ABSENT-OK: zero is a real last-synced time\nv = d.get("k", 0)')) == 0)
check("and without it the same line fires",
      len(hits('v = d.get("k", 0)')) == 1)

print("\nthe None exclusion did NOT move the published figure, and that was measured")
print("  267 hits before the exclusion, 267 after -- this tree uses no None-defaults,")
print("  so the count stands; the detector would have over-counted on a tree that did.")

print("\nAND THE SELF-TEST MUST COVER BOTH SCANS")
import inspect                                                   # noqa: E402
_src = inspect.getsource(avu.self_test)
check("self_test() exercises scan_defaults, not only scan_source",
      "scan_defaults" in _src,
      "a --self-test that prints PASS for half its surface is the defect it scans for")

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
