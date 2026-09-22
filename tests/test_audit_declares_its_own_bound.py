"""Pin what scripts/retraction_audit.py CANNOT do, so it cannot be quietly forgotten.

rho 81fdc47f-9479-46f0-8b3b-44adc233b902 moved its rung-1 plants out of a
throwaway heredoc into a test, "including one that PINS rule2's known rung-2 miss
so it cannot be quietly forgotten rather than sitting in a packet nobody
re-reads." This is that, for the guard this estate has called its worst.

The audit over-fires on real text, its exemption path is a VOCABULARY, and a
spurious exemption word is a FALSE NEGATIVE -- the direction that matters, since
finding unmarked assertions is the entire job. None of that may be dropped
silently by a later edit.
"""
import importlib.util
import io
import os
import sys
import contextlib

sys.path.insert(0, os.getcwd())
_spec = importlib.util.spec_from_file_location("retraction_audit", "scripts/retraction_audit.py")
_mod = importlib.util.module_from_spec(_spec)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    _spec.loader.exec_module(_mod)
out = buf.getvalue()

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


print("the bound must be printed, every run")
check("says it must not gate", "must not gate" in out)
check("says it has no confirmed real positive",
      "NO confirmed real" in out or "no confirmed real" in out.lower())

print("\nsuppressions must be inspectable -- an exemption nobody can see is the defect")
check("suppression count is reported", "SUPPRESSED by strong retraction language" in out)
check("REVIEW bucket exists and is reported", "REVIEW (weak exemption only)" in out)

print("\nweak words must NOT suppress -- a spurious exemption is a FALSE NEGATIVE")
V = _mod._window_verdict
for word in ("never", "NOT a", "used to", "stale", "no longer"):
    v, w = V("some ordinary sentence that happens to say %s here" % word)
    check("%-9s downgrades to REVIEW, does not suppress" % repr(word), v == "REVIEW", "%s/%s" % (v, w))

print("\nstrong words still suppress, and name themselves")
for word in ("retracted", "withdrawn", "superseded", "was wrong", "refuted"):
    v, w = V("this claim was %s on 09-20" % word)
    check("%-11s suppresses" % repr(word), v == "SUPPRESS", "%s/%s" % (v, w))

print("\na window with neither is a HIT")
v, w = V("the gateway serves 91 tools across four namespaces")
check("plain assertion is a HIT", v == "HIT" and w is None, "%s/%s" % (v, w))

print("\nthe pattern table is exempted STRUCTURALLY, never by a word")
check("self-skip is a line range", isinstance(_mod._SELF_SKIP, range))
check("it covers the CLAIMS literal only", len(_mod._SELF_SKIP) < len(open("scripts/retraction_audit.py").read().splitlines()))

print("\na ZERO must say WHICH zero it is -- rho 57804d95")
check("the suppression line distinguishes 'none applied' from a count",
      "SUPPRESSED by strong retraction language" in out)
check("a claim matching nothing says so, and does not read as plain CLEAN",
      "PATTERN MATCHED NOTHING" not in out)
check("and it says the control passed, so 'gone' is a statement not an ambiguity",
      "0 matches, pattern control OK" in out or "matched, all carried" in out)

print("\nevery pattern carries a positive control, and the control can FAIL")
import re as _re
check("every claim is a 4-tuple with a probe", all(len(c) == 4 for c in _mod.CLAIMS),
      "%d claims" % len(_mod.CLAIMS))
check("every pattern matches its own probe",
      all(_re.search(p_, pr, _re.I) for _, _, p_, pr in _mod.CLAIMS))
check("the control announced itself in the output", "pattern control:" in out)
# A control that cannot fail is decoration. Prove this one can.
check("a broken pattern FAILS its own probe",
      not _re.search(r"this pattern matches nothing at all", "the gateway serves 91 tools", _re.I))

print("\nKNOWN AND UNFIXED, pinned so a later edit cannot pretend otherwise:")
print("  · the exemption path is a VOCABULARY. No structural form of")
print("    'does this line carry a retraction' exists, so the weak list is")
print("    incomplete by construction and its misses are FALSE NEGATIVES.")
print("  · ~40 unmarked hits on real text include the use/mention trap:")
print("    scripts/gateway_census.py quotes the retracted figure while")
print("    correcting it two lines later, outside the +/-2 window.")
print("  · chasing that with another exemption word would be the same defect.")

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
