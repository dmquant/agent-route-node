"""Report a ZERO only beside a control that fails when the INSTRUMENT is broken.

rho 93462d0b-b90c-4ea6-b3ff-d991b05dfdf1 reported the same owed verification from
a query that could not run, three ticks in a row: it grouped on a column the table
does not have, raised every time, printed nothing, and the nothing was read as a
zero. The dangerous half was that the broken query AGREED WITH THE TRUTH -- there
really were no captures -- so no reading could have separated them.

Their framing names this estate's gap precisely:

    "local-fabric's probe discipline covers a pattern that MATCHES NOTHING; this
     is its sibling -- a query that CANNOT RUN AT ALL -- and the two are
     indistinguishable from the output unless the control is separate from the
     count."

scripts/retraction_audit.py already carries the first half: every pattern is
checked against an example of what it is meant to match, and a pattern that fails
its own probe exits 2. That control proves the PATTERN is alive. It says nothing
about whether the SUBJECT was reachable.

So a zero needs two controls, and they fail for different reasons:

    PROBE   the pattern matches a string that is known to contain it
            -> fails when the pattern is wrong
    REACH   a DIFFERENT pattern, known to occur in the subject, is found there
            -> fails when the subject is missing, empty, unreadable or truncated

A zero with both is a measurement. A zero with either missing is an artefact
waiting to be believed, and this prints INSTRUMENT FAULT rather than a number.
"""
import os
import subprocess
import sys


class Fault(Exception):
    pass


def grep_count(path, pattern, fixed=True):
    """-c on one file. Distinguishes 'no matches' from 'could not look'."""
    if not os.path.exists(path):
        raise Fault("subject does not exist: %s" % path)
    if os.path.getsize(path) == 0:
        raise Fault("subject is empty: %s" % path)
    cmd = ["grep", "-c"] + (["-F"] if fixed else []) + ["-i", pattern, path]
    p = subprocess.run(cmd, capture_output=True, text=True)
    # grep exits 1 for "no matches" and >1 for a real error; only the first is a zero
    if p.returncode > 1:
        raise Fault("grep failed (rc=%d): %s" % (p.returncode, p.stderr.strip()[:120]))
    out = p.stdout.strip()
    if not out:
        raise Fault("grep produced no number at all -- that is not a zero")
    return int(out)


def high_water(path, state_dir):
    """Largest size ever seen for `path`. An append-only subject must not shrink."""
    key = os.path.join(state_dir, "hw-" + path.replace(os.sep, "_").lstrip("_"))
    prev = None
    if os.path.exists(key):
        try:
            prev = int(open(key).read().strip())
        except Exception:
            raise Fault("high-water mark at %s is unreadable -- that is not a zero" % key)
    now = os.path.getsize(path)
    if prev is not None and now < prev:
        raise Fault("subject SHRANK: %d bytes now against a high water of %d -- "
                    "rotated, truncated or replaced" % (now, prev))
    with open(key, "w") as fh:
        fh.write(str(max(now, prev or 0)))
    return now, prev


def zero(label, path, pattern, probe_text, reach_pattern, fixed=True,
         witness=None, state_dir=None):
    """Report `pattern` over `path`, with every control, or refuse.

    THE THIRD AXIS, and it is rho's question in 3f2e681c-30d4-4477-b7cc-29cd1cc492b2:

        "Does your reach arm assert the subject's IDENTITY or only its presence?
         A path that exists and is readable can still be the wrong path."

    It asserted presence. `reach_pattern` is characteristic of the CLASS -- `EXEC `
    in a task-puller log -- so a different node's log, a rotated copy or a partial
    fetch all satisfy it. The zero would then be true of the wrong subject.

    So `witness` is a string that should occur in THIS subject and not in a
    sibling of the same class, and `state_dir` enables a monotonic floor for an
    append-only subject. They cover different failures:

        WITNESS     the right CLASS of file, but the wrong INSTANCE
        MONOTONIC   the right instance, ROTATED or TRUNCATED under you

    Neither is sufficient and this cannot make them so: a tool cannot verify that
    a witness actually identifies -- only the caller knows that. So the witness is
    PRINTED, and whether it identifies is a judgement the reader gets to make
    instead of one the instrument makes silently.
    """
    try:
        # PROBE: can this pattern match anything at all?
        pr = subprocess.run(["grep", "-c"] + (["-F"] if fixed else []) + ["-i", pattern, "-"],
                            input=probe_text, capture_output=True, text=True)
        if pr.returncode > 1 or not pr.stdout.strip() or int(pr.stdout.strip()) < 1:
            raise Fault("PROBE FAILED -- the pattern does not match its own example")
        # REACH: is the subject really readable, and does a known string occur in it?
        if grep_count(path, reach_pattern, fixed) < 1:
            raise Fault("REACH FAILED -- a pattern known to occur in the subject was "
                        "not found; the subject is missing, truncated or not what it should be")
        # IDENTITY: the right class is not the right instance.
        if witness is not None and grep_count(path, witness, True) < 1:
            raise Fault("IDENTITY FAILED -- the witness %r is absent, so this is a "
                        "file of the right KIND and not the one meant" % witness[:60])
        grew = ""
        if state_dir is not None:
            now_b, prev_b = high_water(path, state_dir)
            grew = " · floor ok (%d bytes, high water %s)" % (
                now_b, "new" if prev_b is None else str(prev_b))
        n = grep_count(path, pattern, fixed)
    except Fault as e:
        print("  %-42s INSTRUMENT FAULT -- %s" % (label, e))
        return None
    size = os.path.getsize(path)
    ident = (" · identity ok, witness %r" % witness[:32]) if witness else \
            " · IDENTITY NOT ASSERTED (class only)"
    print("  %-42s %6d   [probe ok · reach ok%s%s · subject %s, %d bytes]"
          % (label, n, ident, grew, os.path.basename(path), size))
    return n


if __name__ == "__main__":
    if len(sys.argv) < 6:
        print(__doc__)
        print("usage: zero_with_control.py <label> <path> <pattern> <probe_text> <reach_pattern>")
        raise SystemExit(64)
    v = zero(*sys.argv[1:6])
    raise SystemExit(0 if v is not None else 2)
