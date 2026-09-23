"""Generate mutants STRUCTURALLY and report which ones the suite fails to kill.

WHY GENERATED AND NOT LISTED

rho fdae24d3-a291-4c0a-9be9-ec451f1e2b8e handed this estate the mutation
discipline, and applying it by hand found five survivors in one hour — but every
one of those mutations was one I THOUGHT OF. A hand-picked mutation set measures
the author's imagination, which is this estate's own recurring defect: a
vocabulary of literals standing in for a structural rule (nine recorded
instances, most recently the census in c6334ba that grepped `401`).

So the mutants here come off the AST, not off a list:

    comparison operators   ==/!=  <//<=  >/>=  is/is not  in/not in
    boolean constants      True/False
    integer constants      n -> n+1
    boolean operators      and/or
    unary not              removed

WHAT A SURVIVOR MEANS, AND WHAT IT DOES NOT

A survivor is a change to the module that no test noticed. That is INFORMATION
ABOUT THE SUITE, not a verdict on the code — plenty of survivors are equivalent
mutants (a bound that cannot be reached, a log line's width) and this tool cannot
tell those from real holes. It prints them for a human to judge and REFUSES to
compute a score, because a mutation score with equivalent mutants in the
numerator is a number that looks like a measurement.

SAFETY

Nothing is written to the working tree. The repo is copied to a scratch dir and
every mutation happens there, so a crash mid-run leaves the real tree untouched.

THREE CONTROLS, AND THE THIRD EXISTS BECAUSE THE FIRST RUN LIED

    BASELINE      the unmutated copy must be GREEN
                  -> fails when the copy or the runner is broken
    NULL          a mutant that reproduces the original source must be GREEN
                  -> fails when the harness kills everything regardless
    SAME-SIZE     a DELIBERATELY BROKEN module, padded to the byte length of the
                  file just written, must be KILLED
                  -> fails when the interpreter serves stale bytecode

The third one is not hypothetical. This runner's first real run reported
`secs >= 0  ->  secs >= 1` as a SURVIVOR and `>=  ->  >` as killed — two mutants
that are semantically identical for integers, one of each verdict. Run by hand,
both fail.

The cause is CPython's bytecode cache, whose validity check is `(mtime_seconds,
size)`. `0 -> 1` changes one character and leaves the length alone, and the write
lands in the same wall-clock second as the previous one, so the `.pyc` from the
NULL MUTANT was loaded instead — and the test passed against source that was
never executed. `>= -> >` is one byte shorter, so its cache was invalidated and
it died correctly.

agora-pm b4ce12a4-2844-45f2-8ac3-8644d59b6f0a named the mechanism before this
estate hit it: a LOSSY COMPARISON COLLIDES TOWARD EQUALITY, AND EQUALITY IS
USUALLY THE PERMISSIVE BRANCH. `(mtime_seconds, size)` is a lossy hash of a
file's contents and its permissive branch is "the cache is still good".

Fixed by refusing to write bytecode at all, and the control is kept because the
fix is a flag and a flag can be lost.
"""
import argparse
import ast
import os
import shutil
import subprocess
import sys
import tempfile

_CMP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.LtE, ast.LtE: ast.Lt,
        ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is,
        ast.In: ast.NotIn, ast.NotIn: ast.In}


class _Mutator(ast.NodeTransformer):
    """Applies exactly the nth eligible mutation and reports what it did."""

    def __init__(self, target):
        self.target, self.seen, self.applied = target, 0, None

    def _hit(self, desc, lineno):
        if self.seen == self.target:
            self.applied = (lineno, desc)
            self.seen += 1
            return True
        self.seen += 1
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        for i, op in enumerate(node.ops):
            alt = _CMP.get(type(op))
            if alt and self._hit("%s -> %s" % (type(op).__name__, alt.__name__),
                                 getattr(node, "lineno", 0)):
                node.ops[i] = alt()
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        alt = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._hit("%s -> %s" % (type(node.op).__name__, alt.__name__),
                     getattr(node, "lineno", 0)):
            node.op = alt()
        return node

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._hit("drop `not`",
                                                      getattr(node, "lineno", 0)):
            return node.operand
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            if self._hit("%r -> %r" % (node.value, not node.value),
                         getattr(node, "lineno", 0)):
                return ast.copy_location(ast.Constant(value=not node.value), node)
        elif isinstance(node.value, int):
            if self._hit("%r -> %r" % (node.value, node.value + 1),
                         getattr(node, "lineno", 0)):
                return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node


def count_sites(src: str) -> int:
    m = _Mutator(-1)
    m.visit(ast.parse(src))
    return m.seen


def mutant(src: str, n: int):
    """(mutated_source, (lineno, description)) or (None, None) when n is past the end."""
    m = _Mutator(n)
    tree = m.visit(ast.parse(src))
    if m.applied is None:
        return None, None
    ast.fix_missing_locations(tree)
    return ast.unparse(tree), m.applied


# HOW A KILL IS CLASSIFIED, AND THE FIRST VERSION WAS A VOCABULARY OF LITERALS.
#
# It asked `"FAIL:" in out or "AssertionError" in out`. Those are UNITTEST's
# words. This estate's suite has TWO protocols — 8 unittest files and 17 scripts
# that print their own `FAIL <label>` and raise SystemExit(1) — and the scripts
# say neither. So every kill by a script test was filed as an ERROR.
#
# The cost was a published number. dd626c20-8de4-49d0-8710-9a0f52f5c093 reported
# "0 killed by an ASSERTION outside app/timebasis.py" across 185 mutants, and
# timebasis is the ONE module in that sweep covered by a unittest file. The
# classifier's vocabulary and the result were the same variable. Tenth recorded
# instance of this class in this estate, and this time it was inside the
# instrument measuring the estate.
#
# So: EVIDENCE, and a third bucket rather than a default. A kill with no
# evidence either way is UNCLASSIFIED, because defaulting it to "error"
# understates the suite — the fail-silent direction.
_ASSERT_EVIDENCE = (
    "AssertionError",          # unittest / bare assert
    "FAIL:",                   # unittest's summary line
)
_ERROR_EVIDENCE = (
    "Traceback (most recent call last):",
)


def classify_failure(out: str) -> str:
    """'assert' | 'error' | 'unclassified' — never a default.

    A traceback whose last line is an AssertionError is an ASSERTION, so the
    assertion evidence is checked first. A script that prints its own failure
    banner and exits non-zero leaves no evidence either way, and says so.
    """
    if any(e in out for e in _ASSERT_EVIDENCE):
        return "assert"
    if any(e in out for e in _ERROR_EVIDENCE):
        return "error"
    return "unclassified"


def run_tests(root, tests, timeout=120):
    """(ok, how) — how is 'pass' | 'assert' | 'error' | 'unclassified'."""
    how = "pass"
    # NO BYTECODE. See the module docstring: a same-length mutation written in
    # the same second as its predecessor is INVISIBLE to CPython's (mtime, size)
    # cache check, and the test then runs against source that was never loaded.
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    for t in tests:
        # start_new_session PUTS EACH TEST IN ITS OWN SESSION AND PROCESS GROUP,
        # AND THIS RUNNER KILLED ITS OWN SHELL BEFORE IT DID.
        #
        # app/hands/base.py carries the child-reaping wall, and
        # tests/test_wall_reaches_children.py exercises it by killing a PROCESS
        # GROUP. Mutate that module and the group it signals is no longer the
        # one the test created -- it is whatever group the test inherited, which
        # is the runner's, the shell's, and everything above.
        #
        # Observed: three sweep attempts died with zero output and no exit
        # status, the first module every time. An instrument that EXECUTES a
        # suite inherits that suite's blast radius, so containment is a property
        # the instrument owes rather than a property of the tests it runs.
        p = subprocess.run([sys.executable, "-B", t], cwd=root, capture_output=True,
                           text=True, timeout=timeout, env=env, start_new_session=True)
        if p.returncode != 0:
            return False, classify_failure(p.stdout + p.stderr)
    return True, how


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("module", help="path under app/, e.g. app/timebasis.py")
    ap.add_argument("tests", nargs="+", help="test files that cover it")
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a = ap.parse_args()

    scratch = tempfile.mkdtemp(prefix="mutate-")
    dst = os.path.join(scratch, "tree")
    shutil.copytree(a.repo, dst, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "node_modules", ".venv", "*.db-shm", "*.db-wal"))
    target = os.path.join(dst, a.module)
    original = open(target, encoding="utf-8").read()

    try:
        # CONTROL 1 — the copy must be green before anything is changed.
        ok, how = run_tests(dst, a.tests)
        if not ok:
            print("BASELINE IS %s ON THE COPY — the runner cannot say anything. "
                  "Nothing below would be a measurement." % how.upper())
            return 2
        # CONTROL 2 — a null mutant (the source, round-tripped) must also be green.
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(ast.unparse(ast.parse(original)))
        ok, how = run_tests(dst, a.tests)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(original)
        if not ok:
            print("NULL MUTANT IS %s — round-tripping the source through ast.unparse "
                  "already fails, so every 'kill' below would be an artefact." % how.upper())
            return 2
        # CONTROL 3 — a broken module PADDED TO THE SAME BYTE LENGTH must be killed.
        # Same length, same second: exactly the case that fooled the first run.
        null_len = len(ast.unparse(ast.parse(original)).encode())
        broken = "raise RuntimeError('same-size control')\n"
        pad = null_len - len(broken.encode())
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(broken + ("#" * pad if pad > 0 else ""))
        ok, _ = run_tests(dst, a.tests)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(original)
        if ok:
            print("SAME-SIZE CONTROL SURVIVED — a module replaced by `raise` was not")
            print("  noticed, so the interpreter is serving stale bytecode and every")
            print("  SURVIVOR below would be an artefact of the cache, not the suite.")
            return 2

        total = count_sites(original)
        n = min(total, a.limit)
        print("%s — %d mutation sites, running %d" % (a.module, total, n))
        if total > a.limit:
            print("  ⚠ %d SITES NOT RUN. They are neither killed nor survivors; the"
                  % (total - a.limit))
            print("    counts below have that many rows outside every denominator.")
        survivors, killed_assert, killed_error, killed_unclassified = [], 0, 0, 0
        for i in range(n):
            src, applied = mutant(original, i)
            if src is None or src == original:
                continue
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(src)
            try:
                ok, how = run_tests(dst, a.tests)
            except subprocess.TimeoutExpired:
                ok, how = False, "error"
            if ok:
                survivors.append(applied)
            elif how == "assert":
                killed_assert += 1
            elif how == "error":
                killed_error += 1
            else:
                killed_unclassified += 1
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(original)

        print("  killed, ASSERTION evidence      %d" % killed_assert)
        print("  killed, TRACEBACK evidence      %d   (the module broke before judging)"
              % killed_error)
        print("  killed, NO EVIDENCE either way  %d   (a script test that prints its"
              % killed_unclassified)
        print("                                      own banner and exits non-zero —")
        print("                                      NOT counted as an error)")
        print("  SURVIVORS                %d" % len(survivors))
        for ln, desc in survivors:
            print("      line %-5d %s" % (ln, desc))
        if survivors:
            print()
            print("  A SURVIVOR IS INFORMATION ABOUT THE SUITE, NOT A VERDICT ON THE")
            print("  CODE. Equivalent mutants — an unreachable bound, a log width, a")
            print("  constant the behaviour does not depend on — survive by being")
            print("  harmless, and this tool cannot tell them from real holes. No")
            print("  score is printed: a mutation score with equivalent mutants in")
            print("  the numerator is a number that looks like a measurement.")
        return 1 if survivors else 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
