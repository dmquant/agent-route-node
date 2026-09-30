"""The completion callback must say which model ran, and who says so.

agent-route `1dd94f33-c088-4ae2-8f58-4885eabf54a2`: it already stores node-reported
metrics on node_tasks and tasks, and 0 of 1,362 tasks in 24 h carried any, because
_post_result sent only exitCode and resultText. The opencode hand had been computing
tokens and cost into HandResult.metrics the whole time; they stopped at the node.
"""
import asyncio
import os
import unittest
from unittest import mock

from app import task_puller as tp


class _Client:
    def __init__(self):
        self.bodies = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.bodies.append((url, json))
        return mock.Mock(status_code=200)


class TestCompletionSaysWhichModelRan(unittest.TestCase):

    def test_both_callbacks_carry_the_metrics(self):
        c = _Client()
        m = {"model": "x/y", "model_source": "config", "hand": "opencode"}
        asyncio.run(tp._post_result(c, "http://w", "k", "t1", 0, "out", m))
        self.assertEqual(len(c.bodies), 2)
        for _, body in c.bodies:
            self.assertEqual(body["metrics"], m)

    def test_the_task_model_wins_and_is_config(self):
        with mock.patch.dict(os.environ, {"OPENCODE_MODEL": "env/default"}):
            m = tp._model_metrics("opencode", "task/model", {"tokens_total": 5})
        self.assertEqual((m["model"], m["model_source"], m["tokens_total"]), ("task/model", "config", 5))

    def test_the_env_default_is_config(self):
        with mock.patch.dict(os.environ, {"CLAUDE_DEFAULT_MODEL": "opus"}):
            m = tp._model_metrics("claude", None)
        self.assertEqual((m["model"], m["model_source"]), ("opus", "config"))

    def test_an_unknown_model_stays_unknown(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            m = tp._model_metrics("codex", None)
        self.assertIsNone(m["model"])
        self.assertEqual(m["model_source"], "cli-default")

    def test_agy_never_claims_the_model_it_ignored(self):
        m = tp._model_metrics("agy", "gpt-5")
        self.assertIsNone(m["model"])
        self.assertEqual(m["model_source"], "cli-default")

    def test_a_hand_that_parsed_the_cli_output_is_cli_reported(self):
        m = tp._model_metrics("opencode", "asked/for", {"model": "actually/ran"})
        self.assertEqual((m["model"], m["model_source"]), ("actually/ran", "cli-reported"))


if __name__ == "__main__":
    unittest.main()
