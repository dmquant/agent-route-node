"""The census's first two runs were both wrong, in opposite directions.

RUN 1, SUBSTRING DETECTION, MATCHED ITSELF. `_probes_endpoint` asked
`"urlopen" in src and "jsonrpc" in src`, and the file contains both strings
because they ARE its detector. It reported itself as a credential-probing
artefact and then as the single artefact CARRYING the defect — an instrument
counting its own description of the thing it counts.

RUN 1 ALSO COULD NOT SEE THE DEFECT IT WAS BUILT FOR. The arm detector grepped
for the literal `401`, so an artefact comparing status codes through a VARIABLE
was invisible. whoami_gw.py — the exact file whose control was broken on
2026-09-23 because it probed `initialize` — came back "NONE (no auth verdict in
this file)". Ninth instance in this estate of a vocabulary of literals sitting
in a structural path.

So both halves are structural now, and these are the regressions:

    a file that MENTIONS urlopen and jsonrpc in prose is not an artefact
    a file that issues methods but has no `401` literal is still classified
    a method named only in a DOCSTRING is not a method the artefact issues
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "duplicate_instruments.py")


def run(*paths):
    p = subprocess.run([sys.executable, SCRIPT, *paths], capture_output=True, text=True)
    return p.stdout


class TestCensusSeesItselfCorrectly(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def write(self, name, body):
        p = os.path.join(self.tmp, name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def test_the_census_does_not_count_itself(self):
        """Run 1's headline bug: the detector's own strings matched its source."""
        out = run(SCRIPT)
        self.assertIn("ARTEFACTS IN THE POPULATION PASSED: 0", out)

    def test_prose_about_urlopen_is_not_an_artefact(self):
        p = self.write("prose.py", '"""This module explains urlopen and jsonrpc and'
                                   ' tools/list at length."""\nX = 1\n')
        out = run(p)
        self.assertIn("PASSED: 0", out)

    def test_a_file_with_no_401_literal_is_still_classified(self):
        """Run 1 reported 'NONE (no auth verdict)' for exactly this shape."""
        p = self.write("byvar.py", (
            "import urllib.request, json\n"
            "def go(u, h):\n"
            "    b = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call'}).encode()\n"
            "    r = urllib.request.urlopen(urllib.request.Request(u, data=b, headers=h))\n"
            "    code = r.getcode()\n"
            "    ok = code == 200\n"
            "    return ok\n"))
        out = run(p)
        self.assertIn("PASSED: 1", out)
        self.assertIn("tools/call", out)

    def test_a_method_named_only_in_a_docstring_is_not_issued(self):
        """THIS FIXTURE WAS TOO SMALL TO EXPRESS ITS OWN MUTATION, and the
        mutation run caught it — rho fdae24d3-a291-4c0a-9be9-ec451f1e2b8e §2,
        in this estate within the hour of being published.

        The first version's docstring read "We deliberately do NOT use
        tools/list here." Method matching is EXACT EQUALITY on a string
        constant, so that docstring never equalled "tools/list" and the
        docstring exclusion was never exercised: deleting the exclusion left
        the suite GREEN.

        The only value that separates the two is a docstring whose ENTIRE
        content is a method name. That is contrived — and it is precisely the
        contrivance the code claims to handle, so the fixture has to be it.
        """
        p = self.write("docstr.py", (
            "import urllib.request, json\n"
            "def go(u):\n"
            '    """tools/list"""\n'
            "    b = json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize'}).encode()\n"
            "    return urllib.request.urlopen(urllib.request.Request(u, data=b))\n"))
        out = run(p)
        self.assertIn("PASSED: 1", out)
        line = [l for l in out.splitlines() if "docstr.py" in l and "initialize" in l]
        self.assertTrue(line, out)
        self.assertNotIn("tools/list", line[0])

    def test_an_initialize_only_artefact_is_reported_without_being_accused(self):
        p = self.write("initonly.py", (
            "import urllib.request, json\n"
            "def go(u):\n"
            "    b = json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize'}).encode()\n"
            "    return urllib.request.urlopen(urllib.request.Request(u, data=b))\n"))
        out = run(p)
        self.assertIn("issue NO GATED METHOD", out)
        self.assertIn("A STRUCTURAL FACT, NOT AN ACCUSATION", out)
        self.assertNotIn("CARRIES THE DEFECT", out)

    def test_an_unparseable_file_is_named_not_dropped(self):
        p = self.write("broken.py", "def (:\n")
        out = run(p)
        self.assertIn("COULD NOT BE PARSED", out)
        self.assertIn("broken.py", out)


if __name__ == "__main__":
    unittest.main()
