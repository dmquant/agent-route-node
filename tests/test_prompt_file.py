"""ARC-204: workspace prompt-file fallback for prompts too big for argv."""
import os
import stat
import tempfile
import unittest
from unittest import mock

from app.hands import base


class PromptFileTests(unittest.TestCase):
    def test_threshold(self):
        self.assertFalse(base.prompt_too_big_for_argv("x" * base.STDIN_PROMPT_THRESHOLD))
        self.assertTrue(base.prompt_too_big_for_argv("x" * (base.STDIN_PROMPT_THRESHOLD + 1)))

    def test_stage_and_unlink_direct_mode(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AGENT_ROUTE_HAND_USER": ""}):
            prompt = "é" * 200_000
            path, instr = base.stage_prompt_file(d, prompt)
            self.assertEqual(os.path.dirname(path), d)
            self.assertTrue(os.path.basename(path).startswith(".agent-route-prompt-"))
            self.assertIn(os.path.basename(path), instr)
            self.assertLess(len(instr), 1024)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), prompt.encode("utf-8"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            base.unlink_prompt_file(path)
            self.assertFalse(os.path.exists(path))
            base.unlink_prompt_file(path)  # idempotent
            base.unlink_prompt_file(None)

    def test_split_mode_is_readable_and_stdin_off(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"AGENT_ROUTE_HAND_USER": "hand"}):
            big = "x" * (base.STDIN_PROMPT_THRESHOLD + 1)
            self.assertFalse(base.prompt_via_stdin(big))
            path, _ = base.stage_prompt_file(d, big)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)

    def test_unique_names(self):
        with tempfile.TemporaryDirectory() as d:
            a, _ = base.stage_prompt_file(d, "a")
            b, _ = base.stage_prompt_file(d, "b")
            self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
