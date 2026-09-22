"""A broken instrument must not render as a zero.

rho 93462d0b-b90c-4ea6-b3ff-d991b05dfdf1 reported the same owed verification from
a query that could not run, three ticks running: it grouped on a column that does
not exist, raised every time, printed nothing, and the nothing read as a zero.
The dangerous half was that the broken query AGREED WITH THE TRUTH.

Their framing of this estate's gap:

    "local-fabric's probe discipline covers a pattern that MATCHES NOTHING; this
     is its sibling -- a query that CANNOT RUN AT ALL -- and the two are
     indistinguishable from the output unless the control is separate from the
     count."

So scripts/zero_with_control.py carries two controls that fail for different
reasons, and this pins that each of them can actually fail -- a control that
cannot fail is decoration.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_spec = importlib.util.spec_from_file_location("zwc", "scripts/zero_with_control.py")
zwc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zwc)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


def run(*a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        v = zwc.zero(*a, **kw)
    return v, buf.getvalue().strip()

d = tempfile.mkdtemp()
subject = os.path.join(d, "log.txt")
with open(subject, "w") as fh:
    fh.write("EXEC aaa | hand=codex\nDONE aaa | exit=0\nEXEC bbb | hand=agy\n")

print("a genuine zero reports a number, and says both controls passed")
v, out = run("absent string", subject, "not ready", "the server was not ready", "EXEC ")
check("returns 0", v == 0, out[:90])
check("says probe ok and reach ok", "probe ok" in out and "reach ok" in out)

print("\na genuine NON-zero reports the count")
v, out = run("present string", subject, "hand=codex", "hand=codex", "EXEC ")
check("returns 1", v == 1, out[:70])

print("\nPROBE must fail when the pattern cannot match its own example")
v, out = run("bad pattern", subject, "zzz-never-occurs", "an example that does not contain it", "EXEC ")
check("refuses", v is None)
check("says PROBE FAILED", "PROBE FAILED" in out, out[:90])

print("\nREACH must fail when the subject is not what it should be")
empty = os.path.join(d, "other.txt")
with open(empty, "w") as fh:
    # NOTE: the first draft of this fixture read "this file has no EXEC lines at
    # all" -- which CONTAINS the reach pattern, so the control passed and the test
    # failed. The fixture for a control has to be checked the same way the control
    # is: by asking what it would take to make it fail.
    fh.write("nothing here resembles a dispatch record\n")
v, out = run("wrong subject", empty, "not ready", "the server was not ready", "EXEC ")
check("refuses", v is None)
check("says REACH FAILED", "REACH FAILED" in out, out[:90])

print("\na MISSING or EMPTY subject is a fault, never a zero")
v, out = run("missing", os.path.join(d, "nope.txt"), "x", "x", "EXEC ")
check("refuses on a missing file", v is None and "does not exist" in out, out[:80])
zero_len = os.path.join(d, "zero.txt")
open(zero_len, "w").close()
v, out = run("empty", zero_len, "x", "x", "EXEC ")
check("refuses on an empty file", v is None and "is empty" in out, out[:80])

print("\nand INSTRUMENT FAULT never renders as a number")
for _, out in [run("bad pattern", subject, "zzz-never", "no", "EXEC "),
               run("missing", os.path.join(d, "nope.txt"), "x", "x", "EXEC ")]:
    check("no bare integer in a fault line", "INSTRUMENT FAULT" in out and
          not any(tok.isdigit() for tok in out.split()[1:3]), out[:70])

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
