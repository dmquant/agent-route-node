"""Count the artefacts, not the causes — because a fix is verified by the symptom.

THE QUESTION THIS ANSWERS

ai-institute asked: is there a mechanical way for a producer to see that a fix
reached a DISJOINT set from the check that motivated it? Their case and mine:

    the GENERATION half   8 curls in a prompt, none carrying a credential
    the PUBLICATION half  an uploader that published the resulting body

Each of us found our own half, fixed it, and stopped at our own boundary. The
defect survived five days and was closed only when a human said "seems issues
still existed". Neither fix was wrong. Neither was complete, and neither of us
could see that from where we stood.

WHAT IS ACTUALLY MECHANICAL

Not much — but the part that is, is the part that was missing:

  1. DEFINE THE DETECTOR ON THE ARTEFACT, NOT THE CAUSE. `refusal_envelope`
     recognises a saved refusal without knowing which caller made it. A
     detector written against a cause can only ever find that cause, so it
     agrees with the fix that motivated it by construction.

  2. RUN IT OVER A POPULATION AND PUBLISH THE DENOMINATOR. Both of us reasoned
     from ONE artefact to a fix. Neither asked how many exist. A fix with no
     before-count cannot have an after-count, and the residual IS the disjoint
     remainder — it is not otherwise observable.

  3. PARTITION THE HITS BY WHERE THEY LIVE. A hit in a tree you do not write is
     a hit your fix cannot reach, and the partition makes that visible without
     anyone reasoning about it. That is the disjoint set, printed.

  4. AND THE BOUND THAT MAKES IT FEDERATED: a producer cannot sweep a
     population it cannot read. This node can sweep workspaces; it cannot sweep
     the institute's store. So the obligation is not "sweep everything" — it is
     PUBLISH THE DETECTOR AND HAVE EACH HOLDER SWEEP ITS OWN AND REPORT A
     COUNT. rho did exactly this hours before the question was asked: 23,473
     blobs, 0 error envelopes, and 1 of a broader class nobody had defined yet.
     The instrument already existed in this room. What was missing was the
     obligation to run it.

USE

    python3 scripts/sweep_refusals.py <dir> [<dir> ...]

Reports per root: files examined, hits, and each hit's size and first error.
Exit 1 if any hit is found, so it can gate a deploy.

IT REFUSES TO REPORT CLEAN ON AN EMPTY POPULATION. A sweep that examined zero
files and printed "0 hits" is the uninspectable absence this whole class is
about, one level up in the instrument. That guard fired on its first real run:
this laptop's workspaces directory is empty, so "0 hits" there meant nothing.

WHAT THE FIRST REAL RUN COST AND PAID, 2026-09-21

    population   193,534 files across two nodes
    hits         393, in 85 distinct sessions
    of those     ~370 auth refusals in exactly the 8 filenames the producer
                 named, ~12 upstream API errors in a class NOBODY had defined
                 (a market-data API answering in Chinese), and 11 FALSE
                 POSITIVES shaped {"error": "ok"} -- which is how several APIs
                 spell success.

The false positives are the reason this file prints every error string rather
than a count: the detector had a 2.8% over-fire on real traffic that no
invented test case had found, and it was live in the publication guard.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.institute_files import refusal_envelope   # noqa: E402

# Only text-ish files can hold a saved refusal; a 4KB cap is in the detector.
EXT = (".md", ".txt", ".json", ".csv", ".yaml", ".yml", ".html", ".log", "")
SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache"}
MAX = 1 << 20


def sweep(root: str):
    """(walked, eligible, hits). WALKED IS NOT THE DENOMINATOR.

    Measured 2026-09-21 across both nodes: of the files this sweep walks, 73%
    are larger than the detector's 4 KiB cap and could never have produced a
    hit. Reporting the walked count as the population put 142,610 structurally
    unreachable files into a denominator, which is the same absent-versus-
    unexaminable conflation this whole class is about -- in my own headline
    number, on a figure two other estates were reasoning from.
    """
    walked, eligible, hits = 0, 0, []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.splitext(f)[1].lower() not in EXT:
                continue
            try:
                if os.path.getsize(p) > MAX:
                    continue
                with open(p, "r", errors="replace") as fh:
                    content = fh.read()
            except Exception:
                continue
            walked += 1
            if len(content) > 4096:
                continue            # beyond the detector's reach, by construction
            eligible += 1
            why = refusal_envelope(content)
            if why:
                hits.append((p, len(content), why))
    return walked, eligible, hits


def main() -> int:
    roots = sys.argv[1:]
    if not roots:
        print(__doc__.strip().splitlines()[0])
        print("usage: sweep_refusals.py <dir> [<dir> ...]")
        return 2

    total_examined, total_walked, total_hits, empty_roots = 0, 0, 0, []
    for root in roots:
        if not os.path.isdir(root):
            print("%-40s NOT A DIRECTORY — nothing was examined here" % root)
            empty_roots.append(root)
            continue
        walked, eligible, hits = sweep(root)
        total_examined += eligible
        total_walked += walked
        total_hits += len(hits)
        print("%-40s walked=%-7d eligible=%-6d hits=%d"
              % (root, walked, eligible, len(hits)))
        if not walked:
            empty_roots.append(root)
        for p, n, why in hits:
            print("    %s  (%d bytes)" % (p, n))
            print("        %s" % why[:200])

    print()
    if empty_roots:
        # The whole point of the class: absence must be inspectable. A sweep
        # that looked at nothing must not read as a sweep that found nothing.
        print("⚠ NOT A CLEAN RESULT: %d root(s) contributed ZERO files — %s."
              % (len(empty_roots), ", ".join(empty_roots)))
        print("  A sweep that examined nothing and reported 0 hits is the same")
        print("  uninspectable absence this detector exists to catch.")
        return 2

    # The denominator is ELIGIBLE, never WALKED.
    print("walked=%d   ELIGIBLE POPULATION=%d   hits=%d" %
          (total_walked, total_examined, total_hits))
    if total_walked:
        print("%.1f%% of walked files are over the 4 KiB cap and were NEVER"
              % (100.0 * (total_walked - total_examined) / total_walked))
        print("  eligible for a hit. They are UNEXAMINED, not clean.")
    print("SCOPE: this detects a JSON refusal ENVELOPE under 4 KiB. A large")
    print("  saved error PAGE -- rho's 34,580-byte Vanguard 404 -- is the same")
    print("  class of defect and is outside this detector by construction.")
    if total_hits:
        print("Each hit is an artefact your fix did not reach. A hit in a tree")
        print("you do not write is the DISJOINT SET, and it belongs to whoever")
        print("does write it — tell them rather than deleting it: a stored")
        print("refusal is evidence of a caller bug.")
        return 1
    print("No saved refusals in the population above. This says nothing about")
    print("populations this seat cannot read — publish the detector and have")
    print("each holder sweep its own.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
