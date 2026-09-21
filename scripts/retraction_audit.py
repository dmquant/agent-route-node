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

# (label, what I retracted it TO, regex for the ORIGINAL ASSERTION)
CLAIMS = [
    ("agy expands ${VAR}", "agy does NOT expand; it needs a literal",
     r"agy.{0,60}expand|expand.{0,40}\$\{|\$\{VAR\}.{0,40}(work|expand)"),
    ("governed coverage 3 of 4", "4 of 4 since codex reached ros-gateway",
     r"\b3\s*(of|/)\s*4\b|three of (the )?four"),
    ("all three header forms return 200", "measured against tools/list, which needs no auth",
     r"all three header forms|three header forms.{0,30}200"),
    ("the list is exhaustive / do not search", "scoped SCOPE/CONTENTS sections",
     r"list is exhaustive|[Dd]o not search the filesystem|searching the filesystem will not"),
    ("58 argument names as a live figure", "75 as of 2026-09-21T14:47Z; needs an as-of",
     r"\b58\b.{0,40}(argument|name)|(argument|name).{0,40}\b58\b"),
    ("three namespaces behind the gateway", "four, and vibe is live with 17 tools",
     r"three namespaces|3 namespaces"),
    ("vibe is a sentinel / not a namespace", "vibe is live: 17 tools, 21 args",
     r"vibe.{0,60}(sentinel|not a (real )?namespace|unavailable)|sentinel.{0,40}vibe"),
    ("install_gate.verify trap", "withdrawn for a recording proxy",
     r"\btrap\b"),
    ("75 tools as a live figure", "91 as of 2026-09-21T14:47Z",
     r"\b75\s+tools\b"),
    ("pull env identical on both nodes", "four of six values differ",
     r"identical on both|same on both nodes"),
]

RETRACTION_MARK = re.compile(
    r"retract|withdraw|superseded|CORRECTED|corrected|was wrong|no longer|"
    r"used to|stale|NOT a|never|refut|as of 2026|as-of", re.I)

targets = []
for base in (ROOT, MEM):
    for dirpath, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", "node_modules", ".venv", "venv")]
        for f in files:
            if f.endswith((".md", ".json", ".py", ".txt")):
                targets.append(os.path.join(dirpath, f))

print("files searched: %d  (repo + memory)" % len(targets))
print()
total_hits = 0
for label, corrected_to, pat in CLAIMS:
    rx = re.compile(pat, re.I)
    hits = []
    for path in targets:
        try:
            lines = open(path, errors="replace").read().splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines):
            if not rx.search(line):
                continue
            window = "\n".join(lines[max(0, i - 2):i + 3])
            if RETRACTION_MARK.search(window):
                continue                      # asserted AND marked -> not a hit
            hits.append((os.path.relpath(path, os.path.dirname(ROOT)), i + 1,
                         line.strip()[:120]))
    total_hits += len(hits)
    flag = "CLEAN" if not hits else "%d UNMARKED SITE(S)" % len(hits)
    print("%-42s %s" % (label, flag))
    if hits:
        print("      now: %s" % corrected_to)
        for p, n, l in hits[:6]:
            print("      %s:%d  %s" % (p, n, l))
print()
print("TOTAL unmarked assertions of retracted claims: %d" % total_hits)
