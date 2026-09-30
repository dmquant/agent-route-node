"""Is a claim I retracted still asserted somewhere I can edit?

epistates: "A retraction is not a section edit -- it is a search." They found
3 sites asserting a retracted result and had fixed 1 at the moment they called
it retracted.

agora-pm's warning about their own first attempt: the patterns were written
FROM THE CORRECTION ENTRIES, so of course they matched the correction entries --
a confirming number from a matcher written after knowing the answer.

So every pattern below is written for the CLAIM AS ORIGINALLY ASSERTED, and a
line is only a HIT if it asserts and does not also carry a retraction marker on
the same line or the two lines around it. The corrections are what I am trying
NOT to match.
"""
import os, re, subprocess

ROOT = "/Users/daoming/prog/automations/llm/agent-route-node"
MEM = os.path.expanduser("~/.claude/projects/-Users-daoming-prog-automations-llm/memory")

# (label, what I retracted it TO, regex for the ORIGINAL ASSERTION, a PROBE)
#
# The PROBE is an example of the assertion as originally made, and it is a
# positive control: a pattern that does not match its own probe is BROKEN, which
# is a different thing from a claim that is gone, and without it the two are
# indistinguishable from the output. rho 57804d95-ed76-49ef-b3fe-dd3203a873d5
# discarded a whole run when its control failed on the same pass -- "the control
# is the only reason the first number is not in this message."
CLAIMS = [
    ("agy expands ${VAR}", "agy does NOT expand; it needs a literal",
     r"agy.{0,60}expand|expand.{0,40}\$\{|\$\{VAR\}.{0,40}(work|expand)",
     "the agy registry will expand ${ROS_GATEWAY_MCP_KEY} at load"),
    ("governed coverage 3 of 4", "4 of 4 since codex reached ros-gateway",
     r"\b3\s*(of|/)\s*4\b|three of (the )?four",
     "governed coverage is 3 of 4 hands"),
    # The retraction stands; its stated REASON was over-general and is corrected
    # here. Measured 2026-09-22 from sg01: ros-gateway's tools/list requires auth
    # (real key -> 92 tools, garbage key -> 401, no key -> 401). The no-auth fact
    # was about the INSTITUTE endpoint, not this one.
    ("all three header forms return 200", "all three were measured against tools/list, which "
     "exercises no credential on the institute endpoint; ros-gateway's tools/list DOES require auth",
     r"all three header forms|three header forms.{0,30}200",
     "all three header forms returned 200 against the gateway"),
    ("the list is exhaustive / do not search", "scoped SCOPE/CONTENTS sections",
     r"list is exhaustive|[Dd]o not search the filesystem|searching the filesystem will not",
     "the list is exhaustive; do not search the filesystem"),
    ("58 argument names as a live figure", "75, still 75 as of 2026-09-22T04:52Z",
     r"\b58\b.{0,40}(argument|name)|(argument|name).{0,40}\b58\b",
     "the gateway serves 58 distinct argument names"),
    ("three namespaces behind the gateway", "four, and vibe is live with 17 tools",
     r"three namespaces|3 namespaces",
     "there are three namespaces behind the gateway"),
    ("vibe is a sentinel / not a namespace", "vibe is live: 17 tools, 21 args",
     r"vibe.{0,60}(sentinel|not a (real )?namespace|unavailable)|sentinel.{0,40}vibe",
     "vibe is a sentinel, not a real namespace"),
    ("install_gate.verify trap", "withdrawn for a recording proxy",
     r"\btrap\b",
     "install_gate.verify stands up a trap and counts the hits"),
    # This corrected_to was itself stale within the day: 91 became 92 when
    # institute__research_projects_list arrived. A correction is a claim and
    # decays like one, so the ledger's own values now carry an as-of.
    ("75 tools as a live figure", "92 as of 2026-09-22T04:52Z (was 91; +1 institute)",
     r"\b75\s+tools\b",
     "the gateway serves 75 tools to this key"),
    ("91 tools as a live figure", "92 as of 2026-09-22T04:52Z",
     r"\b91\s+tools\b",
     "the gateway serves 91 tools to this key"),
    ("pull env identical on both nodes", "four of six values differ",
     r"identical on both|same on both nodes",
     "the pull env is identical on both nodes"),
]

# THE EXEMPTION PATH IS A VOCABULARY, AND THAT IS THIS TOOL'S WORST PART.
#
# rho 445f16ab-e79e-47a1-8d08-78e8ba90eb82 rebuilt a detector specifically to
# escape a vocabulary and left one in its exemption. This file had the same shape
# and worse odds: measured 2026-09-22 over 88 files, the single flat list below
# suppressed 39 windows and PRINTED NONE OF THEM, while reporting 40 hits. A
# suppression population the size of the reported one, entirely invisible.
#
# 25 of those 39 -- 64% -- were suppressed by `NOT a` (14), `never` (7),
# `used to` (3) and `stale` (1): words that appear in ordinary prose and carry no
# retraction meaning. Only 9 came from unambiguous language.
#
# DIRECTION, since it decides what this may be used for:
#   a MISSING exemption word  -> false positive: an already-marked correction is
#                                reported as an unmarked assertion. Noisy, safe.
#   a SPURIOUS exemption word -> FALSE NEGATIVE: a genuinely unmarked assertion is
#                                exempted because a neighbouring line said "never".
#                                That is this tool's whole job, silently not done.
# So the weak words no longer suppress. They downgrade to REVIEW, which is printed.
#
# This is a linter a person reads. It must not gate.
_STRONG = re.compile(
    r"retract|withdraw|superseded|corrected|was wrong|refut|as of 2026|as-of", re.I)
_WEAK = re.compile(r"no longer|used to|stale|NOT a|never", re.I)


def _window_verdict(window):
    """(verdict, the word that decided it). SUPPRESS only on strong language."""
    m = _STRONG.search(window)
    if m:
        return "SUPPRESS", m.group(0)
    m = _WEAK.search(window)
    if m:
        return "REVIEW", m.group(0)
    return "HIT", None

# The CLAIMS table above QUOTES every pattern it searches for, so this file
# matches itself on every claim. That is the use/mention trap, and the exemption
# for it must be structural or it is the same defect twice: the range is computed
# from this source, not recognised by a word.
_SELF = os.path.abspath(__file__)
_src = open(_SELF).read().splitlines()
_claims_start = next(i for i, l in enumerate(_src) if l.startswith("CLAIMS = ["))
_claims_end = next(i for i in range(_claims_start, len(_src)) if _src[i].rstrip() == "]")
_SELF_SKIP = range(_claims_start + 1, _claims_end + 2)   # 1-indexed line numbers


def _is_own_pattern_table(path, lineno):
    return os.path.abspath(path) == _SELF and lineno in _SELF_SKIP


targets = []
for base in (ROOT, MEM):
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", "node_modules", ".venv", "venv")]
        for f in files:
            if f.endswith((".md", ".json", ".py", ".txt")):
                targets.append(os.path.join(dirpath, f))

print("files searched: %d  (repo + memory)" % len(targets))

# EVERY PATTERN IS CONTROLLED BEFORE IT IS TRUSTED.
#
# rho 57804d95-ed76-49ef-b3fe-dd3203a873d5 asked whether a zero here says WHICH
# zero it is. It did not -- and making it say so surfaced two claims matching
# nothing at all, which this could then not separate into "the claim is gone" and
# "the regex is broken". The probe separates them: a pattern that fails its own
# example is broken, full stop, and no result from this run may be read as clean.
# rho discarded an entire run on exactly this ground and was right to: "the
# control is the only reason the first number is not in this message."
_broken = [lbl for lbl, _, pat, probe in CLAIMS if not re.search(pat, probe, re.I)]
if _broken:
    print("PATTERN CONTROL FAILED for %d claim(s): %s" % (len(_broken), ", ".join(_broken)))
    print("A pattern that does not match its own example cannot report a clean result.")
    raise SystemExit(2)
print("pattern control: %d of %d patterns match their own example" % (len(CLAIMS), len(CLAIMS)))
print()
total_hits = 0
total_review = 0
_unreadable = {}
suppressed_by = {}
for label, corrected_to, pat, probe in CLAIMS:
    rx = re.compile(pat, re.I)
    hits, review, raw_matches, suppressed_here = [], [], 0, 0
    for path in targets:
        try:
            lines = open(path, errors="replace").read().splitlines()
        except Exception as _e:
            # THE AUDIT'S OWN DENOMINATOR DROPPED ROWS. Found by
            # scan_dropped_rows, which was built to answer rho 445121ed and then
            # pointed at this estate: a target that could not be opened left this
            # loop touching none of hits/review/raw_matches, so it was absent from
            # `files searched` and from `TOTAL unmarked assertions: 0` alike.
            # The file whose job is checking claims was making an uninspectable one.
            _unreadable.setdefault(path, type(_e).__name__)
            continue
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            if _is_own_pattern_table(path, i + 1):
                continue
            raw_matches += 1
            window = "\n".join(lines[max(0, i - 2):i + 3])
            verdict, word = _window_verdict(window)
            where = (os.path.relpath(path, os.path.dirname(ROOT)), i + 1,
                     line.strip()[:120])
            if verdict == "SUPPRESS":
                suppressed_here += 1
                suppressed_by[word.lower()] = suppressed_by.get(word.lower(), 0) + 1
            elif verdict == "REVIEW":
                review.append(where + (word,))
            else:
                hits.append(where)
    total_hits += len(hits)
    total_review += len(review)
    # A ZERO MUST SAY WHICH ZERO IT IS.
    #
    # rho 57804d95-ed76-49ef-b3fe-dd3203a873d5: "a guard with no exemption to
    # report and one that forgot to report read identically." Same shape here, one
    # level up. CLEAN used to mean two different things:
    #   the pattern matched N lines and every one carried retraction language
    #       -> the claim is asserted in several places and all are marked. Good.
    #   the pattern matched NOTHING
    #       -> the claim may be gone, or the wording may have drifted, or the
    #          regex may be broken. A broken pattern reads CLEAN forever, and that
    #          is a FALSE NEGATIVE in a tool whose whole job is finding what was
    #          forgotten.
    # This cannot tell those two apart either -- but it can stop hiding that it
    # cannot, which is the difference between an unknown and an unknown unknown.
    if hits or review:
        flag = "%d unmarked, %d review" % (len(hits), len(review))
    elif raw_matches:
        flag = "CLEAN  (%d matched, all carried retraction language)" % raw_matches
    else:
        # The probe passed above, so the pattern is not broken. That leaves one
        # reading, and it is now a statement rather than an ambiguity.
        flag = "CLEAN  -- 0 matches, pattern control OK: the claim is gone from the corpus"
    print("%-42s %s" % (label, flag))
    if hits or review:
        print("      now: %s" % corrected_to)
    if len(hits) > 6:
        print("      (showing 6 of %d)" % len(hits))
    for p, n, l in hits[:6]:
        print("      HIT     %s:%d  %s" % (p, n, l))
    if len(review) > 6:
        print("      (showing 6 of %d)" % len(review))
    for p, n, l, w in review[:6]:
        print("      REVIEW  %s:%d  [exempted only by %r] %s" % (p, n, w, l))
print()
if _unreadable:
    print("⚠ %d TARGET(S) COULD NOT BE READ -- neither hit nor miss, never examined:"
          % len(_unreadable))
    for _p, _k in sorted(_unreadable.items()):
        print("    %s  (%s)" % (_p, _k))
    print("  Any unmarked assertion in them is invisible to the total below.")
else:
    print("every target was read: %d of %d -- the total below has a full denominator"
          % (len(targets), len(targets)))
print("TOTAL unmarked assertions: %d   REVIEW (weak exemption only): %d" % (total_hits, total_review))
# An exemption nobody can see is the defect this repo keeps finding in other
# people's instruments. Print what was suppressed and on whose authority.
_total_suppressed = sum(suppressed_by.values())
print("SUPPRESSED by strong retraction language: %s"
      % ("NONE APPLIED -- no window carried retraction language" if not _total_suppressed
         else "%d  (%s)" % (_total_suppressed,
                            ", ".join("%s=%d" % kv for kv in
                                      sorted(suppressed_by.items(), key=lambda kv: -kv[1])))))
print()
print("BOUND: this matcher has ~40 false fires on real text and NO confirmed real")
print("positive. It is a linter a person reads. It must not gate.")
