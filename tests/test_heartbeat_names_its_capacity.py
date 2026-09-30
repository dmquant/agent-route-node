"""The heartbeat must carry the capacity its load is measured against.

agent-route `4f0dfb7a-4235-4c60-a77c-13f27ab8c93f`: the Worker (b98aa521) now calls a node
"full" when current_load >= max_concurrent, but registration hard-codes 3 while the puller
gates on NODE_MAX_CONCURRENT. A node set to anything else was judged against the wrong bar,
and at capacity the puller stops pulling, so "full" and "dead" looked the same.
"""
import importlib
import os
import unittest
from unittest import mock


class TestHeartbeatNamesItsCapacity(unittest.TestCase):

    def test_the_body_carries_the_pullers_own_limit(self):
        with mock.patch.dict(os.environ, {"NODE_MAX_CONCURRENT": "5"}):
            import app.task_puller as tp
            importlib.reload(tp)
            from app import edge_register
            body = edge_register._heartbeat_body(2)
        self.assertEqual(body, {"activeTasks": 2, "maxConcurrent": 5})
        self.assertEqual(body["maxConcurrent"], tp.MAX_CONCURRENT)

    def test_the_loop_sends_that_body(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "edge_register.py")).read()
        self.assertIn("json=_heartbeat_body(active_tasks)", src)


if __name__ == "__main__":
    unittest.main()
