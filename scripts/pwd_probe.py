"""Does a child spawned by this node's get_cli_env actually see the right PWD?

`2fd4bfa` fixed the class -- asyncio's `cwd=` changes the real working directory
and NOT `PWD`, so a CLI that trusts `PWD` resolves its project to the daemon's
directory. LFAB-110 then verified the fix two ways, and NEITHER was a live test:
the line is in the source, and no artifact has been written since.

rho 12049fc3-fb6c-4eb9-bf38-46f037b1dc4d reproduced the class and found the trap
that makes a live test necessary AND easy to get wrong:

    a POSIX SHELL REPAIRS ITS OWN PWD AT STARTUP.

So `sh -c 'echo $PWD'` returns clean whether or not the bug is present, and an
estate audited that way gets a false all-clear. The probe must be a child that
READS THE ENVIRONMENT VARIABLE.

Three arms, and the two controls are not optional -- a probe whose control cannot
fail is not measuring anything:

    SUBJECT    a child spawned with get_cli_env(cwd=ws)    PWD should be ws
    CONTROL A  the same child with the env INHERITED       must DISAGREE, or the
                                                           probe cannot see the bug
    CONTROL B  a shell with the same broken env            LOOKS CLEAN -- the trap

    python3 scripts/pwd_probe.py

Spawns three short-lived children in a temp directory. Writes nothing else.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# STRING EQUALITY IS THE WRONG TEST, and this probe reported a false alarm with it
# on its first run. macOS symlinks /var to /private/var, so PWD carries the logical
# path and getcwd() the resolved one: they name one directory and compare unequal.
CHILD = ("import os;"
         "p=os.environ.get('PWD') or '';c=os.getcwd();"
         "print('%s|%s' % (p, bool(p) and os.path.realpath(p)==os.path.realpath(c)))")


def same_dir(pwd: str, cwd: str) -> bool:
    """True when the two name the same directory, symlinks resolved."""
    if not pwd or not cwd:
        return False
    return os.path.realpath(pwd) == os.path.realpath(cwd)


async def _spawn(argv, env, ws):
    p = await asyncio.create_subprocess_exec(
        *argv, cwd=ws, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    return out.decode("utf-8", "replace").strip()


async def main():
    from app.hands.base import get_cli_env
    ws = tempfile.mkdtemp(prefix="pwd_probe_")
    print("daemon cwd  %s" % os.getcwd())
    print("workspace   %s" % ws)

    env = get_cli_env(cwd=ws)
    subject = await _spawn([sys.executable, "-c", CHILD], env, ws)
    ctrl_a = await _spawn([sys.executable, "-c", CHILD], dict(os.environ), ws)
    ctrl_b = await _spawn(["/bin/sh", "-c", 'echo "$PWD|$(pwd)"'], dict(os.environ), ws)

    s_pwd, s_ok = subject.rsplit("|", 1)
    a_pwd, a_ok = ctrl_a.rsplit("|", 1)
    b_pwd, b_cwd = ctrl_b.rsplit("|", 1)

    print()
    print("  SUBJECT    get_cli_env(cwd=ws)   PWD=%s  same_dir=%s" % (s_pwd, s_ok))
    print("  CONTROL A  inherited env         PWD=%s  same_dir=%s" % (a_pwd, a_ok))
    print("  CONTROL B  sh, inherited env     PWD=%s  agrees=%s" % (b_pwd, b_pwd == b_cwd))
    print()

    ok = s_ok == "True"
    control_can_fail = a_ok == "False"
    trap = b_pwd == b_cwd
    if not control_can_fail:
        print("INSTRUMENT FAULT: control A did not disagree, so this probe cannot")
        print("  see the defect it exists for. The result below means nothing.")
        return 2
    if not trap:
        print("NOTE: control B agreed too -- the shell did not repair PWD here, so")
        print("  rho's trap does not reproduce on this platform. Not a fault.")
    print("%s: a child reading PWD sees %s" % (
        "PASS" if ok else "FAIL", "its workspace" if ok else "the daemon's directory"))
    print("  and control A shows the probe WOULD have caught the defect.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
