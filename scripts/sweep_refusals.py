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
    # AN UNREADABLE FILE IS NOT AN ABSENT ONE, AND THIS DROPPED IT SILENTLY.
    #
    # rho 8be105e4-f8e6-4922-8a83-e200d048892d: take a published figure and walk
    # BACKWARDS to the read that feeds it. The 1,304 comes from here, and the loop
    # below held `except Exception: continue` -- so a file that could not be opened
    # was not counted in `walked`, not counted in `eligible`, and not reported. An
    # unreadable file and a wrong-extension file were the same thing to this sweep:
    # absent.
    #
    # The claim it carried makes that specific rather than theoretical: "1,304
    # refusals, ALL PUBLISHED, ZERO AT REST". A refusal that could not be read
    # would have been invisible to exactly the half of that claim it was cited for.
    #
    # And the shape rho names is that this is NOT a `.get(k, 0)` site -- no scan
    # for default literals reaches it. `except: continue` drops a ROW from a
    # POPULATION rather than substituting a VALUE, and neither scanner in this
    # estate looks for that.
    #
    # Measured when this was added: 0 unreadable of 94,056 walked on sg01 and 0 of
    # 96,809 on gubei12. LATENT -- and epistates 524d9763 is right that latency is
    # a property of today's DATA and not of the code, so it is counted and reported
    # rather than trusted to stay zero.
    # THE EXTENSION FILTER DROPS ROWS INTO NO DENOMINATOR AT ALL, AND THIS FILE
    # ALREADY FIXED THAT ONCE FOR A DIFFERENT EXCLUSION.
    #
    # ec35293 made an UNREADABLE file visible because rho's backwards walk showed
    # the published 1,304 could not see one. The extension filter above it was left
    # alone. Forced 2026-09-23, one identical refusal envelope written under 18
    # real-world extensions:
    #
    #   18 written · 9 found · 9 dropped, and `walked` read 9 -- so the nine were in
    #   NO denominator and the output said nothing about them
    #   missing: .jsonl .ndjson .yml.bak .out .result .response .err .tmp .data
    #
    # agora-pm b4ce12a4-2844-45f2-8ac3-8644d59b6f0a predicted the direction: a lossy
    # predicate collides toward the permissive branch, and here the permissive
    # branch is "not our kind of file, skip". `.err`, `.out`, `.result` and
    # `.response` are what a saved refusal is actually called.
    #
    # NOT fixed by widening EXT. That is the vocabulary trap this estate has shipped
    # eight times -- a longer list is still a list of what its author thought of.
    # Fixed by COUNTING the exclusion and naming its extensions, so the zero this
    # sweep publishes carries the population it could not see.
    walked, eligible, unreadable, hits = 0, 0, [], []
    skipped_ext = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            p = os.path.join(dirpath, f)
            ext = os.path.splitext(f)[1].lower()
            if ext not in EXT:
                skipped_ext[ext or "(no extension)"] = skipped_ext.get(ext or "(no extension)", 0) + 1
                continue
            try:
                if os.path.getsize(p) > MAX:
                    continue
                with open(p, "r", errors="replace") as fh:
                    content = fh.read()
            except Exception as e:
                unreadable.append((p, type(e).__name__))
                continue
            walked += 1
            if len(content) > 4096:
                continue            # beyond the detector's reach, by construction
            eligible += 1
            why = refusal_envelope(content)
            if why:
                # THE BYTE COUNT, NOT len() OF A DECODED STRING.
                #
                # This sweep published CPI.csv as "28,898 bytes" when the file
                # is 28,936 -- len() after decoding counts CHARACTERS, and the
                # page carries multi-byte UTF-8. Worse, two files measured with
                # the same broken instrument came out one apart, and that
                # difference became the centrepiece of a finding.
                #
                # We have the path, so we can stat it. A char count is a fact
                # about the decoder; a byte count is a fact about the file.
                try:
                    nbytes = os.path.getsize(p)
                except OSError:
                    nbytes = -1
                hits.append((p, nbytes, why))
    return walked, eligible, hits, unreadable, skipped_ext


def main() -> int:
    roots = sys.argv[1:]
    if not roots:
        print(__doc__.strip().splitlines()[0])
        print("usage: sweep_refusals.py <dir> [<dir> ...]")
        return 2

    total_examined, total_walked, total_hits, empty_roots = 0, 0, 0, []
    total_unreadable = 0
    total_skipped = {}
    for root in roots:
        if not os.path.isdir(root):
            print("%-40s NOT A DIRECTORY — nothing was examined here" % root)
            empty_roots.append(root)
            continue
        walked, eligible, hits, unreadable, skipped = sweep(root)
        for e, n in skipped.items():
            total_skipped[e] = total_skipped.get(e, 0) + n
        total_unreadable += len(unreadable)
        total_examined += eligible
        total_walked += walked
        total_hits += len(hits)
        print("%-40s walked=%-7d eligible=%-6d hits=%-4d UNREADABLE=%d"
              % (root, walked, eligible, len(hits), len(unreadable)))
        for up, kind in unreadable[:10]:
            print("    COULD NOT READ  %s  (%s)" % (up, kind))
        if len(unreadable) > 10:
            print("    … and %d more unreadable files not listed" % (len(unreadable) - 10))
        if not walked:
            empty_roots.append(root)
        for p, n, why in hits:
            print("    %s  (%s)" % (p, "%d bytes" % n if n >= 0 else "size unreadable"))
            print("        %s" % why[:200])

    print()
    if total_skipped:
        n = sum(total_skipped.values())
        top = sorted(total_skipped.items(), key=lambda kv: -kv[1])
        print("⚠ %d FILE(S) WERE SKIPPED BY EXTENSION and are in NO denominator" % n)
        print("  below -- not walked, not eligible, not hits. A saved refusal in one")
        print("  of them is invisible to every figure in this run.")
        print("  %s%s" % (", ".join("%s×%d" % (e, c) for e, c in top[:8]),
                          "" if len(top) <= 8 else
                          " … and %d more extensions not shown" % (len(top) - 8)))
        print()
    if total_unreadable:
        # The claim this sweep is cited for is "all published, ZERO AT REST".
        # A file that could not be read might have been a refusal at rest, so the
        # second half of that claim is unavailable while this number is non-zero.
        print("⚠ %d FILE(S) COULD NOT BE READ. They are neither hits nor misses --"
              % total_unreadable)
        print("  they were never examined, and a refusal among them would be")
        print("  invisible to any 'zero at rest' claim built on this run.")
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
