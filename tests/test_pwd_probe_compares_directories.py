"""The probe's own comparison, which produced a false alarm on its first run.

Measured on sg01: the FIXED arm reported disagreement because PWD carried
/var/folders/... and getcwd() returned /private/var/folders/... -- macOS symlinks
/var to /private/var. String equality says those differ; they are one directory.

A probe that cries wolf on a correct fix is the same defect as a check that
cannot be cleared by fixing what it flags, which this estate shipped an hour
earlier in scripts/auth_gate_position.py.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = open(os.path.join(_ROOT, "scripts", "pwd_probe.py"), errors="replace").read()
_ns = {"os": os}
exec(compile(_src[_src.index("def same_dir("):_src.index("async def _spawn(")],
             "pwd_probe.py", "exec"), _ns)
same_dir = _ns["same_dir"]


class TestSameDir(unittest.TestCase):

    def test_a_symlinked_prefix_is_the_same_directory(self):
        """The live case: /var vs /private/var on macOS."""
        real = tempfile.mkdtemp(prefix="pwdcmp_real_")
        link = os.path.join(tempfile.mkdtemp(prefix="pwdcmp_link_"), "alias")
        os.symlink(real, link)
        self.assertNotEqual(link, real, "the strings must differ or this proves nothing")
        self.assertTrue(same_dir(link, real))

    def test_different_directories_are_not_the_same(self):
        a = tempfile.mkdtemp(prefix="pwdcmp_a_")
        b = tempfile.mkdtemp(prefix="pwdcmp_b_")
        self.assertFalse(same_dir(a, b))

    def test_an_absent_pwd_is_never_the_same(self):
        """An unset PWD must not compare equal to anything -- that is the defect
        state, and an empty string realpaths to the process's own cwd."""
        cwd = os.getcwd()
        self.assertFalse(same_dir("", cwd))
        self.assertFalse(same_dir(None, cwd))

    def test_identical_paths_agree(self):
        cwd = os.getcwd()
        self.assertTrue(same_dir(cwd, cwd))


if __name__ == "__main__":
    unittest.main()
