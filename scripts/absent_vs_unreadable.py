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


def self_test() -> int:
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
    print("\nSELF-TEST %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


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
    for p in sorted(files):
        try:
            src = open(p, errors="replace").read()
        except Exception:
            print("%s: COULD NOT READ — which is this whole class, in the scanner" % p)
            continue
        high, low = scan_source(src, p)
        total_h += len(high)
        total_l += len(low)
        for ln, caught, detail in high:
            print("SHAPE-HIGH %s:%d  except %s -> %s" % (p, ln, caught, detail))
        for ln, caught, detail in low:
            print("shape-low  %s:%d  except %s -> %s" % (p, ln, caught, detail))

    print("\nfiles=%d  SHAPES(high)=%d  shapes(low)=%d" % (len(files), total_h, total_l))
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
