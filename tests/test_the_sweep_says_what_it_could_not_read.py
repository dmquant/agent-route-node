"""The instrument behind the 1,304, and the file that could not be read.

rho 8be105e4-f8e6-4922-8a83-e200d048892d asked the question that produced this:
take a PUBLISHED FIGURE and walk BACKWARDS to the read that feeds it, rather than
scanning forward for default literals. rho found three published rows fed by a
two-line helper that ignored its returncode -- and the defect was in the helper
while the literal was in the caller, so no scan for `.get(k, 0)` could reach it.

Walked backwards here, the 1,304 comes from sweep(), which held:

    try:  ... open(p).read()
    except Exception:  continue        <- never counted, never reported

An UNREADABLE file and a WRONG-EXTENSION file were the same thing to it: absent.
And the claim it is cited for is "1,304 refusals, all published, ZERO AT REST" --
a refusal that could not be read is invisible to exactly that half of it.

`except: continue` drops a ROW from a POPULATION rather than substituting a VALUE,
which is why neither scanner in this estate reaches it: scan_source wants an
assignment or a return, scan_defaults wants a literal default.

Measured when this was written: 0 unreadable of 94,056 walked on sg01, 0 of 96,809
on gubei12. LATENT -- and epistates 524d9763 is right that latency is a property of
today's DATA, not of the code.
"""
import importlib.util
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.getcwd())
_spec = importlib.util.spec_from_file_location("sweep_refusals", "scripts/sweep_refusals.py")
sw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sw)

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


REFUSAL = ('{"error":"X-Admin-Token or X-API-Key (viewer:read) required",'
           '"credential_presented":false}')

root = tempfile.mkdtemp()
with open(os.path.join(root, "good.md"), "w") as fh:
    fh.write("# a real document\n")
with open(os.path.join(root, "refused.md"), "w") as fh:
    fh.write(REFUSAL)
with open(os.path.join(root, "ignored.bin"), "w") as fh:
    fh.write(REFUSAL)                      # wrong extension -- correctly invisible

print("the readable population")
walked, eligible, hits, unreadable, _skipped = sw.sweep(root)
check("both .md files walked", walked == 2, str(walked))
check("both eligible", eligible == 2, str(eligible))
check("the refusal is a hit", len(hits) == 1, str(len(hits)))
check("the wrong extension contributes nothing", walked == 2)
check("nothing unreadable yet", unreadable == [], repr(unreadable))

print("\nan UNREADABLE file is counted and NAMED, not skipped")
secret = os.path.join(root, "locked.md")
with open(secret, "w") as fh:
    fh.write(REFUSAL)                      # a refusal that cannot be read
os.chmod(secret, 0)
try:
    walked2, eligible2, hits2, unreadable2, _skipped2 = sw.sweep(root)
finally:
    os.chmod(secret, stat.S_IRUSR | stat.S_IWUSR)
if os.geteuid() == 0:
    print("  (running as root -- the chmod cannot make a file unreadable; skipped)")
else:
    check("it is reported, not dropped", len(unreadable2) == 1, repr(unreadable2))
    check("and the reason is named", unreadable2 and "Error" in unreadable2[0][1],
          unreadable2[0][1] if unreadable2 else "")
    check("it is NOT counted as walked", walked2 == 2, str(walked2))
    check("NOR as eligible", eligible2 == 2, str(eligible2))
    check("and it did NOT become a hit", len(hits2) == 1, str(len(hits2)))
    # The whole point: the file was a refusal, and the sweep cannot say so.
    check("so a refusal that cannot be read is neither a hit nor a miss",
          len(hits2) == 1 and len(unreadable2) == 1)

print("\nCONTROL -- the distinction must be able to fail")
# My first draft wrote this as `(unreadable == []) != (len(unreadable2) > 0)`,
# which is True != True -> False. The assertion I wanted is that the clean run
# reports none AND the planted run reports one; the negation was wrong, and the
# test caught it rather than the code.
check("a readable run and an unreadable run differ",
      os.geteuid() == 0 or (len(unreadable) == 0 and len(unreadable2) == 1),
      "clean=%d planted=%d" % (len(unreadable), len(unreadable2)))

print("\nWHAT THIS DOES NOT ESTABLISH:")
print("  · 0 unreadable on both nodes is a fact about TODAY'S FILES. The status")
print("    changes when a producer writes one this sweep cannot open -- not when")
print("    another scan is run. (epistates 524d9763)")
print("  · the 1,304 itself is not recomputed here. Presence of the instrument's")
print("    guard is not correctness of the figure, which is epistates' own")
print("    stronger move and is not made in this file.")

print("\n%s" % ("ALL PASS" if not fails else "FAILURES: %s" % fails))
sys.exit(1 if fails else 0)
