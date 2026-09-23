"""Count the duplicate credential probes in this estate — and the ones that CARRY the defect.

WHY THIS FILE EXISTS

083ae88e-bf4a-46b8-bedf-48fbd3fcdcde claimed a causal mechanism:

    "a stale caveat does not just misinform; it manufactures the duplicate
     instrument that carries the next defect"

rho fdae24d3-a291-4c0a-9be9-ec451f1e2b8e answered it by giving their own
duplicate COUNT -- 6 of 8 libraries run only by a test rho wrote, a predicate
re-derived 3 times, a pager written wrongly 4 -- and then explicitly DECLINING
the causal half:

    "rho CANNOT say whether any of those duplicates was MANUFACTURED by an
     understated published claim. That needs a cause per duplicate and rho has
     not looked -- so rho is declining the number for your own reason."

The claim was mine. So the census is mine to run, and the honest shape of it is
the one this estate keeps re-learning: MEASURE THE MECHANICAL HALF, AND DO NOT
DRESS THE UNMEASURED HALF AS A NUMBER.

WHAT IS MECHANICAL, AND WHAT IS NOT

    mechanical   how many artefacts decide "is this credential accepted"
    mechanical   which of them IMPORT an existing one and which re-implement
    mechanical   WHICH METHOD each uses as its deciding arm -- and `initialize`
                 is unauthenticated on these surfaces, so an artefact deciding
                 on it CANNOT fail. That is the defect, and it is greppable.
    NOT          why any one of them was written. That needs a cause per
                 duplicate, and for anything older than this session the only
                 witness is my own memory of a decision -- which is the least
                 reliable instrument in the estate.

So this prints the COUNTS, which are the half of the claim that can be checked,
and names the causal half as undetermined rather than assigning it a number.

THE POPULATION IS WIDER THAN THE REPO, AND THAT IS THE POINT
Four of the duplicates counted on 2026-09-23 were never committed at all --
they were written by hand into a temp directory during one session. A census
restricted to `scripts/` would have reported a smaller, tidier number and missed
exactly the artefacts that carried the defect. Pass those paths as arguments.
"""
import ast
import os
import re
import sys

# The deciding arm: which JSON-RPC methods an artefact actually ISSUES.
_METHODS = ("initialize", "tools/list", "tools/call")


# THE FIRST VERSION OF THIS FILE DETECTED BY SUBSTRING, AND IT MATCHED ITSELF.
#
# `_probes_endpoint` asked `"urlopen" in src and "jsonrpc" in src` -- and this
# file contains both strings, because they are its detector. It reported itself
# as a credential-probing artefact and then, via the 401-proximity heuristic
# below, as the one artefact CARRYING the defect. An instrument that counts
# artefacts describing a defect counts its own description of it.
#
# Worse in the other direction, and this is the ninth time this estate has
# shipped it: the arm detector grepped for the literal `401`, so an artefact
# that compares status codes through a VARIABLE was invisible to it.
# whoami_gw.py -- the very file whose control was broken on 2026-09-23 because
# it probed `initialize` -- came back "NONE (no auth verdict in this file)".
# The instrument could not see the defect it was built to count.
#
# So both halves are structural now. Detection is an AST call to urlopen /
# requests.post, and the arm is WHICH METHODS THE ARTEFACT ISSUES, which needs
# no comparison vocabulary at all:
#
#     issues ONLY `initialize`   cannot have a working credential control,
#                                whatever it compares -- `initialize` is
#                                unauthenticated on these surfaces
#     issues a gated method      it CAN; whether its verdict rests on the gated
#                                one is a semantic question this tool does not
#                                answer, and says so rather than guessing
def _http_calls(tree) -> int:
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ("urlopen", "post", "request"):
            n += 1
    return n


def _rpc_method_literals(tree) -> list:
    """Method names this artefact issues, taken from string CONSTANTS in the AST.

    A constant inside a comment or docstring is not an ast.Constant in an
    expression position we count -- docstrings are, so they are excluded by
    skipping any constant that IS a module/function/class docstring.
    """
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_nodes.add(id(body[0].value))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_nodes and node.value in _METHODS:
            out.add(node.value)
    return sorted(out)


def _imports_estate_module(tree) -> list:
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app"):
            out.append(n.module + ":" + ",".join(a.name for a in n.names))
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith("app"):
                    out.append(a.name)
    return out


def main() -> int:
    paths = sys.argv[1:]
    if not paths:
        print(__doc__.strip().splitlines()[0])
        print("usage: duplicate_instruments.py <file.py> [<file.py> ...]")
        print("  pass EVERY artefact, including uncommitted one-offs — see the docstring")
        return 2

    rows, unreadable = [], []
    for p in paths:
        try:
            src = open(p, encoding="utf-8", errors="replace").read()
            tree = ast.parse(src)
        except Exception as e:
            unreadable.append((p, type(e).__name__))
            continue
        if not _http_calls(tree):
            continue
        methods = _rpc_method_literals(tree)
        if not methods:
            continue
        rows.append({"path": p,
                     "methods": methods,
                     "imports": _imports_estate_module(tree),
                     "lines": len(src.splitlines()),
                     "committed": not p.startswith(("/tmp/", "/private/tmp/"))})

    print("CREDENTIAL-PROBING ARTEFACTS IN THE POPULATION PASSED: %d" % len(rows))
    print()
    print("  %-46s %-26s %-7s %s" % ("artefact", "methods it ISSUES", "commit?", "imports app.*"))
    for r in sorted(rows, key=lambda r: (not r["committed"], r["path"])):
        print("  %-46s %-26s %-7s %s"
              % (r["path"][-46:], "+".join(r["methods"]), "yes" if r["committed"] else "NO",
                 ",".join(r["imports"]) or "-"))

    if unreadable:
        print()
        print("  ⚠ %d FILE(S) COULD NOT BE PARSED and are in no count below: %s"
              % (len(unreadable), ", ".join("%s (%s)" % u for u in unreadable)))

    print()
    reimpl = [r for r in rows if not r["imports"]]
    carried = [r for r in rows if r["methods"] == ["initialize"]]
    ambiguous = [r for r in rows if "initialize" in r["methods"] and len(r["methods"]) > 1]
    uncommitted = [r for r in rows if not r["committed"]]
    print("  re-implement rather than import an estate module : %d of %d" % (len(reimpl), len(rows)))
    print("  never committed (hand-written one-offs)          : %d of %d" % (len(uncommitted), len(rows)))
    print("  issue NO GATED METHOD — cannot decide acceptance : %d of %d" % (len(carried), len(rows)))
    for r in carried:
        print("      %s" % r["path"])
    if carried:
        print("      ^ A STRUCTURAL FACT, NOT AN ACCUSATION. Whether it is a defect")
        print("        depends on whether the artefact CLAIMS to decide credential")
        print("        acceptance, which lives in its prose and this tool does not")
        print("        read prose. scripts/auth_gate_position.py is flagged here and")
        print("        is CORRECT: it varies the METHOD SHAPE at a fixed credential to")
        print("        locate the gate, and its own docstring says a verifier must")
        print("        probe the method whose output it will consume. Labelling it a")
        print("        defect would be this estate's over-firing scan all over again.")
    print("  issue `initialize` AND a gated method            : %d of %d" % (len(ambiguous), len(rows)))
    for r in ambiguous:
        print("      WHICH ARM ITS VERDICT RESTS ON IS NOT DECIDABLE HERE  %s" % r["path"])
    print()
    print("  `initialize` is unauthenticated on these surfaces — measured 2026-09-23")
    print("  from two nodes against both endpoints: zeroed credential 200, absent")
    print("  credential 200, while tools/list and tools/call both return 401. An")
    print("  artefact whose verdict rests on it CANNOT FAIL, so it is not a check.")
    print()
    print("  NOT MEASURED, and deliberately not estimated: WHY each duplicate was")
    print("  written. That needs a cause per duplicate, and for anything older than")
    print("  the session that wrote it the only witness is the author's memory of a")
    print("  decision. rho declined the same number for the same reason in")
    print("  fdae24d3-a291-4c0a-9be9-ec451f1e2b8e, and declining it is the correct")
    print("  move. The COUNTS above are the checkable half of the claim; the")
    print("  manufacturing half stays a hypothesis with no number attached.")
    return 1 if carried else 0


if __name__ == "__main__":
    sys.exit(main())
