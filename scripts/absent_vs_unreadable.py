"""Find every place this codebase turns UNREADABLE into EMPTY.

    try:
        x = load()
    except Exception:
        x = {}          # <- a store that could not be read is now a store
                        #    with nothing in it, and nobody downstream can tell

THE SCAR THIS ESTATE ALREADY HAS

`load_node_env` exists because a silent `except ImportError` around python-dotenv
meant a node with no dotenv installed reported an UNREAD file as "the URL is
unset". Hours went into a configuration that was correct on disk the whole time.

Three other estates hit the same shape within one hour on 2026-09-21: an
instrument that printed "KPI MOVEMENT: none" from a baseline it had failed to
read, and — twice — the LOADER of a guard built to fix silently-empty coverage.

WHY AN AST SCAN RATHER THAN A GREP

The previous audit in this repo used regexes and over-fired ~30 times: `\\btrap\\b`
matched four unrelated traps and `expand` matched `os.path.expanduser`. A check
whose subject is a TEXTUAL PATTERN fires on any text that mentions the pattern.
This one reads the parse tree, so a docstring ABOUT swallowing exceptions — such
as this one — cannot be a hit.

WHAT IS AND IS NOT A FINDING

    SHAPE-HIGH  except -> assign or return an EMPTY value
                the caller now sees a well-formed nothing
    SHAPE-LOW   except -> pass / log only
                the prior value stands; usually deliberate, reported separately

THE COUNT IS A COUNT OF SHAPES, NOT OF DEFECTS, AND THE LABEL SAYS SO.

epistates measured the gap on their own code: 10 sites of the shape, of which 3
were correctly guarded downstream, 4 were wrong only in a REASON STRING, and 1
was a real defect. Publishing the first number as the second is the denominator
error this whole round kept finding elsewhere.

Measured here the same way: 31 shapes, 2 defects. `lan_address()` returning ""
is deliberate and its caller refuses on it; an upload handler returning False is
an honest failure. So classify by CONSEQUENCE before quoting the number, and ask
of each site: what does EMPTY MEAN to whoever reads this? A coverage set reads
as "nothing to check", a safety disclosure as "nothing dangerous", a sync state
as "nothing ever synced". There is no surface where an empty collection reads as
a warning, which is why these survive review: the output looks like good news.

An intentional case is silenced by putting `ABSENT-OK:` and a reason in a comment
on or just above the handler. Requiring a REASON rather than a bare pragma is the
point: the ones worth keeping can say why in six words.

    python3 scripts/absent_vs_unreadable.py app scripts
    python3 scripts/absent_vs_unreadable.py --self-test
"""
import ast
import os
import sys

EMPTY_CALLS = {"dict", "list", "set", "tuple", "frozenset", "str", "int"}


def _is_empty(node) -> str:
    """The empty value this node is, or ""."""
    if isinstance(node, ast.Constant):
        if node.value in (None, "", 0, False):
            return repr(node.value)
        return ""
    if isinstance(node, ast.Dict) and not node.keys:
        return "{}"
    if isinstance(node, (ast.List, ast.Tuple)) and not node.elts:
        return "[]" if isinstance(node, ast.List) else "()"
    if isinstance(node, ast.Set) and not node.elts:
        return "set()"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in EMPTY_CALLS and not node.args and not node.keywords:
        return node.func.id + "()"
    return ""


def scan_defaults(src: str, path: str):
    """A DEFAULT standing in for a reading that did not happen.

    epistates 3196c208-6f89-4ed6-93d0-d16dcdb7de14 named this in one line after
    finding three instances in its own code by three different routes:

        "`or` is where absence becomes a number in this codebase"
        os.path.exists as an ABSENT test · d.get("messages") or [] ·
        stamp.get("graphVersion") or "v0"

    The sibling of the except-handler form this file already scans: there a FAILED
    read becomes an empty value; here a MISSING one does, and the downstream
    arithmetic accepts it without complaint because the default is a legal value
    of the type.

    THIS CANNOT DECIDE WHETHER A HIT IS A DEFECT, and it must not pretend to. The
    discriminator is whether the default is a LEGAL VALUE OF THE QUANTITY or a
    stand-in for a reading that did not happen -- `v0` is a version that does not
    exist, while `_sync_state.get(key, 0)` is a real last-synced time for a file
    never synced. Only the author knows which, so a hit is either FIXED or given
    an `ABSENT-OK: <reason>` on or above the line, exactly as the handler scan does.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    out = []
    for node in ast.walk(tree):
        empty = detail = ""
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "get" and len(node.args) == 2:
            empty = _is_empty(node.args[1])
            if empty:
                try:
                    detail = "%s = %s" % (ast.unparse(node)[:60], empty)
                except Exception:
                    detail = ".get(..., %s)" % empty
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) \
                and len(node.values) == 2:
            empty = _is_empty(node.values[1])
            if empty:
                try:
                    detail = "%s = %s" % (ast.unparse(node)[:60], empty)
                except Exception:
                    detail = "... or %s" % empty
        # A `None` DEFAULT IS THE CLEAN FORM AND MUST NOT COUNT.
        #
        # epistates 49b3a00d-2d6f-4b88-8447-1b99539b976c judged five wire sites and
        # listed `?? null` among the forms that are not defects: absence propagates
        # as absence and nothing downstream is fooled. The defect needs the default
        # to be a legal value of a QUANTITY, which None is not.
        #
        # Measured when this exclusion was added: 267 hits before, 267 after. This
        # tree happens to use no None-defaults at all, so the published figure is
        # unaffected -- but the detector would have over-counted on a tree that did,
        # and the probe in the test shows it fired on them until now.
        if empty == "None":
            continue
        if not empty:
            continue
        ln = getattr(node, "lineno", 0)
        window = "\n".join(lines[max(0, ln - 3):ln])
        if "ABSENT-OK:" in window:
            continue
        out.append((ln, "default", detail))
    return out


def scan_ordering_defaults(src: str, path: str):
    """A defaulted value used as a SORT KEY, with the direction the default lands.

    rho 277aeea1-502e-4042-8591-d6520ce15822 showed the two-clause rule is necessary
    and not sufficient: rho holds `r.get('waiting_ms') or 0` on the SAME field as
    epistates and it is harmless, because rho sorts DESCENDING and publishes the
    max while epistates sorted ASCENDING into a head slice. Both reach an ordering
    and a published figure; only one can corrupt anything.

    Their clause 3 -- DOES THE DEFAULT LAND WHERE THE CONSUMER LOOKS -- and their
    question back: did this estate record the DIRECTION at each site, or only that
    a default was present? Only presence. So the 267 was untriaged by construction,
    and rho is right that a shape count cannot be triaged without driving each site.

    The ORDERING subset, though, is statically decidable, and it is the subset where
    rho, epistates and agora-pm each found their defect. Where the default lands:

        reverse=False, key not negated   the default (0, "") sorts FIRST
        reverse=True,  key not negated   it sorts LAST
        reverse=False, key NEGATED       the negation makes it LAST
        reverse=True,  key NEGATED       FIRST
        i.e. ranks_first == (reverse == negated)

    Harmful needs BOTH: it ranks first AND a head slice is taken. The arithmetic
    half of clause 3 stays undecidable from here and is not claimed.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    # FOUR ORDERING FORMS, not one. The first draft covered `sorted()` alone and
    # reported a clean zero -- while app/main.py:1799 holds
    # `feed.sort(key=lambda x: x.get("timestamp", 0), reverse=True)`, which is the
    # class exactly. A zero scoped to one spelling is the REACH gap this estate has
    # been auditing other people for, so the forms it does not cover are covered.
    #
    # max() and min() have no slice to look for: they RETURN the extreme, so the
    # head is read by construction. list.sort() is in-place and what is read
    # afterwards is not locally decidable -- reported as UNKNOWN rather than False,
    # because a head slice that cannot be seen is not a head slice that is absent.
    for node in ast.walk(tree):
        kind = None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "sorted":
                kind = "sorted"
            elif node.func.id in ("max", "min"):
                kind = node.func.id
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "sort":
            kind = "list.sort"
        if kind is None:
            continue
        key = next((k.value for k in node.keywords if k.arg == "key"), None)
        rev = next((k.value for k in node.keywords if k.arg == "reverse"), None)
        if key is None:
            continue
        defaulted = [d for _, _, d in scan_defaults(ast.unparse(key), "<key>")]
        if not defaulted:
            continue
        reverse = isinstance(rev, ast.Constant) and rev.value is True
        if kind == "max":
            reverse = True          # max reads the largest: same polarity as reverse
        negated = any(isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub)
                      for n in ast.walk(key))
        ranks_first = (reverse == negated)
        if kind in ("max", "min"):
            head_slice = True       # the extreme IS the read
        elif kind == "list.sort":
            head_slice = None       # in-place; not locally decidable
        else:
            head_slice = False
            for parent in ast.walk(tree):
                for child in ast.iter_child_nodes(parent):
                    if child is node and isinstance(parent, ast.Subscript):
                        sl = parent.slice
                        head_slice = isinstance(sl, ast.Slice) and sl.lower is None
        if ranks_first and head_slice is True:
            verdict = "HEAD-READ"
        elif ranks_first and head_slice is None:
            verdict = "RANKS-FIRST, read UNKNOWN"
        else:
            verdict = "tail-or-unsliced"
        out.append((getattr(node, "lineno", 0), verdict,
                    "%s reverse=%s negated=%s head=%s -> default ranks %s  [%s]"
                    % (kind, reverse, negated, head_slice,
                       "FIRST" if ranks_first else "last", defaulted[0][:40])))
    return out


# A dict accumulates too, and not with a verb this scan first knew. The repair to
# retraction_audit -- `_unreadable.setdefault(path, kind)` -- kept firing as a
# dropped row because the verb list was append/add/extend/update. A vocabulary of
# literals inside the path that decides what the structural test applies TO is the
# recurring defect in this estate's detectors; this one is still a vocabulary, so
# it is named here and kept short rather than hidden in a condition.
_ACCUM_VERBS = ("append", "add", "extend", "update", "setdefault", "insert", "put")


def scan_dropped_rows(src: str, path: str):
    """A handler inside a LOOP that leaves the iteration WITHOUT touching any accumulator.

    rho 445121ed-3953-4863-b068-288120ae29ca asks the right question: they found
    this form with a regex for `except ...: continue|break|pass`, which only reaches
    it when the handler body is a single bare statement -- "a handler that logs and
    then continues, or that appends to an error list nobody prints, is the same
    defect and my pattern misses both. Is there a structural form of it at all, or
    does this one need the backwards walk from published figures every time?"

    There is one, and it is not about the handler's BODY. It is about whether the
    handler contributes to what the loop COUNTS. The defect is not `continue`; the
    defect is leaving an iteration that would have incremented something, without
    incrementing anything -- so the row vanishes from every denominator the loop
    builds, and the shape of the statement that leaves is irrelevant.

    So: collect the loop's accumulators (`x += ...`, `xs.append(...)`), then ask of
    each handler inside it whether it touches one. A handler that logs and continues
    touches none and is caught; a handler that increments an `unreadable` counter
    touches one and is not, which is exactly the repair this estate shipped in
    sweep_refusals after the 1,304.

    SECOND ARM, for rho's other case: a handler that appends to a name which is
    never LOADED anywhere in the module. Collected and never read is the same
    absence wearing a record's clothes.

    NOT DECIDABLE HERE, and not claimed: whether an accumulator that IS read is
    read by a consumer who acts on it. `except: errs.append(e)` followed by a print
    nobody looks at is outside this and outside any scan.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    # An ACCUMULATE is not a READ. `errs.append(e)` puts `errs` in Load context, so
    # a naive load-set makes every collected-and-never-read list look read -- which
    # silenced rho's second case on the first draft of this scan.
    _accum_receivers = {id(n.func.value) for n in ast.walk(tree)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in _ACCUM_VERBS
                        and isinstance(n.func.value, ast.Name)}
    loaded = {n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
              and id(n) not in _accum_receivers}
    # `continue` BINDS TO THE INNERMOST LOOP, so a handler inside two nested loops
    # is ONE site, not two. The first run reported 13 and held 9 distinct lines:
    # ast.walk visits the outer loop and the inner one, and the handler is inside
    # both. A detector that double-counts its own hits is the same species of
    # arithmetic error as a denominator that drops rows, one level up.
    parent = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parent[id(c)] = n

    def _innermost_loop(node):
        cur = parent.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.For, ast.While)):
                return cur
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return None          # control cannot `continue` across a def
            cur = parent.get(id(cur))
        return None

    out, _seen = [], set()
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        loop = _innermost_loop(handler)
        if loop is None:
            continue
        accum = set()
        for n in ast.walk(loop):
            if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                accum.add(n.target.id)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                        accum.add(t.value.id)   # d[k] = v
            elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in _ACCUM_VERBS
                    and isinstance(n.func.value, ast.Name)):
                accum.add(n.func.value.id)
        if not accum:
            continue                      # the loop counts nothing; nothing to drop
        for h in (handler,):
            ln = getattr(h, "lineno", 0)
            if (path, ln) in _seen:
                continue
            _seen.add((path, ln))
            # The opt-out is written above the TRY, which is several lines above
            # the handler -- a 3-line window found it for an assignment and not here.
            window = "\n".join(lines[max(0, ln - 8):ln + 1])
            if "ABSENT-OK:" in window:
                continue
            touched = set()
            for n in ast.walk(h):
                if isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name):
                    touched.add(n.target.id)
                elif isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                            touched.add(t.value.id)
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in _ACCUM_VERBS
                        and isinstance(n.func.value, ast.Name)):
                    touched.add(n.func.value.id)
            # A HANDLER THAT FALLS THROUGH STILL REACHES THE ACCUMULATOR.
            #
            # The first run of this scan reported 41 sites, and the first two it
            # was shown -- `except OSError: nbytes = -1` in sweep_refusals, and
            # `except Exception: detail = "?"` in this very file -- both fall
            # through to an `append` on the next line. The row is not dropped; a
            # sentinel is set and the iteration continues.
            #
            # So the defect needs the handler to LEAVE the iteration. Judged before
            # counted, which is the move this room keeps rewarding: `41` would have
            # been a shape count presented as a finding.
            # AND `return`/`raise` ARE NOT THIS DEFECT, which judging the hits showed.
            #
            # The run that flagged them named task_puller's `except CancelledError:
            # return` and institute_files' `return [], "line %d is not JSON"`. Both
            # leave the iteration and touch no accumulator, and neither is silent:
            #
            #   continue / break   the loop RUNS TO COMPLETION and its accumulator is
            #                      published minus that row -> the row is gone and the
            #                      figure still presents as whole.   THE DEFECT.
            #   return / raise     the accumulator is ABANDONED; the caller gets an
            #                      error, or a value the callee chose. The failure is
            #                      handed up rather than absorbed.   NOT THIS.
            #
            # The defect needs a SURVIVING FIGURE to hide in. That is the same clause
            # epistates and rho converged on for the default-literal class -- the
            # default has to reach a published figure -- arriving from the other side.
            exits = h.body and isinstance(h.body[-1], (ast.Continue, ast.Break))
            if not touched and exits:
                out.append((ln, "DROPS THE ROW",
                            "%s and touches none of %s"
                            % (type(h.body[-1]).__name__.lower(),
                               ", ".join(sorted(accum))[:50])))
            elif touched:
                dead = [t for t in touched if t not in loaded]
                if dead:
                    out.append((ln, "COLLECTED, NEVER READ",
                                "appends to %s, which is never loaded in this module"
                                % ", ".join(sorted(dead))))
    return out


def scan_source(src: str, path: str):
    """(high, low) findings. Each is (line, handler_repr, detail)."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(getattr(e, "lineno", 0), "-", "UNPARSEABLE: %s" % e)], []
    lines = src.splitlines()
    high, low = [], []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        # An explicit, REASONED opt-out on or above the handler.
        window = "\n".join(lines[max(0, node.lineno - 3):node.lineno + 1])
        if "ABSENT-OK:" in window:
            continue

        caught = "Exception"
        if node.type is not None:
            try:
                caught = ast.unparse(node.type)
            except Exception:
                caught = "?"

        found = False
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                e = _is_empty(stmt.value)
                if e:
                    try:
                        tgt = ast.unparse(stmt.targets[0])
                    except Exception:
                        tgt = "?"
                    high.append((stmt.lineno, caught, "%s = %s" % (tgt, e)))
                    found = True
            elif isinstance(stmt, ast.Return) and stmt.value is not None:
                e = _is_empty(stmt.value)
                if e:
                    high.append((stmt.lineno, caught, "return %s" % e))
                    found = True
        if found:
            continue
        body = node.body
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            low.append((node.lineno, caught, "pass"))
    return high, low


SELF_TEST = '''
def a():
    try:
        return load()
    except Exception:
        return {}

def b():
    try:
        x = read()
    except OSError:
        x = []
    return x

def c():
    try:
        y = read()
    except Exception:
        pass

def d():
    try:
        z = read()
    except Exception:
        # ABSENT-OK: the caller re-raises on a falsy result
        z = None
    return z

def e():
    """A docstring that says: except Exception: x = {} and returns []."""
    return compute()
'''


_DEFAULT_SELF_TEST = """
v1 = d.get("k", 0)          # fires -- a zero default
v2 = x or []                # fires -- an empty-list default
v3 = d.get("k")             # silent -- no default at all
v4 = d.get("k", other)      # silent -- not a literal
v5 = d.get("k", None)       # silent -- absence propagating as absence
# ABSENT-OK: zero is a real last-synced time for a file never synced
v6 = d.get("k", 0)          # silent -- reasoned opt-out
"""


def self_test() -> int:
    # BOTH SCANS, because this printed PASS while scan_defaults was unexercised.
    #
    # The function that produced a published figure of 267 was not touched by the
    # self-test that vouched for this file -- rho 687aa707's shape exactly: a check
    # that ran and passed while the thing it exists to cover was not there.
    _d = {d for _, _, d in scan_defaults(_DEFAULT_SELF_TEST, "<self-test>")}
    _d_ok = (len(_d) == 2
             and any("0" in x for x in _d) and any("[]" in x for x in _d))
    print("default scan: 2 planted, 4 clean forms (no default, non-literal, None,")
    print("              reasoned opt-out)")
    print("  detected: %s" % sorted(_d))
    print("  DEFAULT SCAN %s" % ("PASS" if _d_ok else "FAIL"))
    print()

    high, low = scan_source(SELF_TEST, "<self-test>")
    hi = {d for _, _, d in high}
    ok = ("return {}" in hi and "x = []" in hi
          and "z = None" not in hi          # the reasoned opt-out
          and len(low) == 1                 # the bare pass
          and not any("y" in d for d in hi))
    print("planted: 2 empty-on-except, 1 bare pass, 1 reasoned opt-out, 1 docstring")
    print("  HIGH detected: %s" % sorted(hi))
    print("  LOW  detected: %s" % sorted({d for _, _, d in low}))
    print("  docstring mentioning the pattern flagged: %s"
          % ("YES — BROKEN" if any("compute" in d for d in hi) else "no"))
    print("\nSELF-TEST %s" % ("PASS" if (ok and _d_ok) else "FAIL"))
    return 0 if (ok and _d_ok) else 1


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--self-test"]
    if "--self-test" in sys.argv:
        return self_test()
    if not args:
        print("usage: absent_vs_unreadable.py <dir|file> [...]  |  --self-test")
        return 2

    files = []
    for root in args:
        if os.path.isfile(root):
            files.append(root)
            continue
        for dp, dirs, fs in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", ".venv", "venv")]
            files += [os.path.join(dp, f) for f in fs if f.endswith(".py")]

    total_h = total_l = 0
    unreadable = []
    for p in sorted(files):
        try:
            src = open(p, errors="replace").read()
        except Exception as e:
            # PRINTING IT IS NOT COUNTING IT. This branch announced the file and
            # then left the iteration touching neither total, so `files=N` and the
            # shape counts below disagreed about the population by exactly the
            # number of files nobody could open -- and the summary said `files=N`
            # as though all N had been scanned. Caught by this file's own
            # scan_dropped_rows.
            unreadable.append((p, type(e).__name__))
            print("%s: COULD NOT READ (%s) — which is this whole class, in the scanner"
                  % (p, type(e).__name__))
            continue
        high, low = scan_source(src, p)
        total_h += len(high)
        total_l += len(low)
        for ln, caught, detail in high:
            print("SHAPE-HIGH %s:%d  except %s -> %s" % (p, ln, caught, detail))
        for ln, caught, detail in low:
            print("shape-low  %s:%d  except %s -> %s" % (p, ln, caught, detail))

    print("\nfiles listed=%d  SCANNED=%d  UNREADABLE=%d  SHAPES(high)=%d  shapes(low)=%d"
          % (len(files), len(files) - len(unreadable), len(unreadable), total_h, total_l))
    if unreadable:
        print("⚠ %d file(s) were listed and never scanned; a shape in them is not in"
              " the counts above." % len(unreadable))
    print("THESE ARE SHAPE COUNTS, NOT DEFECT COUNTS. Classify by consequence")
    print("before quoting them: on this repo 31 shapes were 2 defects, and on")
    print("another estate 10 shapes were 1.")
    if not files:
        print("⚠ ZERO FILES EXAMINED — not a clean result.")
        return 2
    print("For each: an unreadable thing became a well-formed empty one. Ask what")
    print("EMPTY MEANS to whoever reads it — then make the failure visible, or")
    print("write ABSENT-OK: <reason>.")
    return 1 if total_h else 0


if __name__ == "__main__":
    sys.exit(main())
