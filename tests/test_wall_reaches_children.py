"""A wall that kills a launcher has not killed the work.

rho reported the shape from its own host (`c423501b-98dd-4e4b-b7c5-d63c5ffebebb`):
the wall fired on time, the launcher died, and the real process ran on for 50
minutes reparented to init -- so the guard's FAILURE read exactly like the guard
never firing.

It applies here because almost nothing this node runs is a leaf process:
claude and codex are `npx` -> node -> the real CLI, and opencode is an npm .bin
shim on the studios. opencode_hand's wall called process.kill() on the launcher.

Measured before the fix, on this machine: 4 lines written at the kill, 10 three
seconds later -- the grandchild outlived the wall that reported killing it.

No network, no credentials, no federation traffic. The child is a python loop
appending to a file.
"""
import os, signal, subprocess, sys, tempfile, time

sys.path.insert(0, os.getcwd())
from app.hands.base import spawn_kwargs, kill_process_tree

fails = []


def check(label, cond, detail=""):
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", label, ("  " + detail) if detail else ""))
    if not cond:
        fails.append(label)


CHILD = """import os, sys, time
p = sys.argv[1]
for i in range(80):
    open(p, "a").write("%d %d\\n" % (i, os.getpid()))
    time.sleep(0.25)
"""


def launch(use_group):
    """A launcher that forks a real grandchild -- `; true` defeats sh's exec
    optimisation, which is what makes this npx-shaped rather than a leaf."""
    d = tempfile.mkdtemp()
    script = os.path.join(d, "child.py")
    log = os.path.join(d, "out.log")
    open(script, "w").write(CHILD)
    kw = spawn_kwargs() if use_group else {}
    p = subprocess.Popen(
        ["/bin/sh", "-c", "%s %s %s; true" % (sys.executable, script, log)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **kw)
    return p, log


def lines(log):
    try:
        return [l for l in open(log).read().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def reap(log):
    """Never leave a grandchild running because a test made one."""
    for l in lines(log):
        try:
            os.kill(int(l.split()[1]), signal.SIGKILL)
        except Exception:
            pass


print("THE FIX — spawn_kwargs() + kill_process_tree()")
p, log = launch(True)
time.sleep(1.5)
at_kill = len(lines(log))
how = kill_process_tree(p)
p.wait()
time.sleep(2.0)
after = len(lines(log))
check("the grandchild ran before the wall", at_kill > 0, "%d lines" % at_kill)
check("the wall reached the grandchild", after == at_kill,
      "%d at kill, %d after" % (at_kill, after))
check("it reports killing a GROUP", "group" in how, how)
reap(log)

print("\nTHE CONTROL — the old pattern, to prove this test can SEE the failure")
p2, log2 = launch(False)
time.sleep(1.5)
at_kill2 = len(lines(log2))
p2.kill()                       # exactly what opencode_hand.py did
p2.wait()
time.sleep(2.0)
after2 = len(lines(log2))
check("the old pattern LEAVES the grandchild running", after2 > at_kill2,
      "%d at kill, %d after" % (at_kill2, after2))
reap(log2)
time.sleep(0.3)

print("\nDEGRADED PATH — a dead process must not raise inside a timeout handler")
p3, log3 = launch(True)
kill_process_tree(p3)
p3.wait()
how3 = kill_process_tree(p3)    # again, on something already gone
check("second kill returns a reason instead of raising", isinstance(how3, str), how3)
reap(log3)

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % ", ".join(fails)))
sys.exit(1 if fails else 0)
