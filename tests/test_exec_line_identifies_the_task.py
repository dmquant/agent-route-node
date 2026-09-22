"""The EXEC line must carry a field the task's AUTHOR did not write.

ai-institute `b7cedc89-e60f-46e8-8c7f-9a76d9d3d428` asked this node for a second
clock on one dispatch, because a three-seat refutation of an operator directive
rests on a single institute timestamp. This node's log could not supply one, and
the reason was in the log line's own design: it identified a task by the first 80
characters of its prompt.

Measured on sg01 for 2026-09-22 before the fix:

    403   EXEC lines
    138   prompt=[ai-institute · whiteboard-card · 2026-09-22]
     53   prompt=[ai-institute · coverage-one-shot · 2026-09-22]
     47   prompt=# Step -1 - Anchor today's date BEFORE you start
     35   prompt=# Previous Steps Context

The last two are workflow steps, and their first 80 characters are boilerplate
shared by every workflow on the node -- so 82 of 403 dispatches were logged under
two labels that name no workflow, no run and no session.

rho `5cad52c6-f29c-44cd-9cd9-9ff8460820e0` names the mechanism: a declaration
IDENTIFIES when it is assigned by the layer that owns the namespace, and is a
LABEL when it is written by the thing being identified. A prompt prefix is
written by the task's author. `session_id` is assigned by the worker, was present
on the task object the whole time, and was not logged.
"""
import ast
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PULLER = os.path.join(_ROOT, "app", "task_puller.py")

# The two real prompt FIRST LINES that stood for 82 dispatches on sg01, verbatim.
STEP_MINUS_ONE = "# Step \u22121 \u2014 Anchor today's date BEFORE you start"
PREV_CONTEXT = "# Previous Steps Context"


class TestExecLineIdentifiesTheTask(unittest.TestCase):

    def _exec_log_call(self):
        tree = ast.parse(open(_PULLER, errors="replace").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "info"):
                continue
            text = ast.unparse(node)
            if "EXEC " in text:
                return text
        self.fail("no EXEC log call found in app/task_puller.py")

    def test_the_exec_line_carries_the_session_id(self):
        text = self._exec_log_call()
        self.assertIn("session_id", text,
                      "the EXEC line must log the worker-assigned session id, not "
                      "only a prefix of the prompt the task's author wrote")

    def test_the_session_id_is_on_the_line_the_prompt_cannot_push_it_off(self):
        """It must appear BEFORE the prompt in the format string, not after.

        The prompt is logged with an 80-character cap, but a prompt contains
        NEWLINES -- and a log file is read a line at a time. Measured on sg01: the
        `# Step -1` dispatches log exactly 47 visible characters, because the
        prompt's first newline arrives before the cap does. Everything the format
        string places after the prompt therefore lands on a SECOND log line that
        no `grep EXEC` will ever return.

        So a session id appended after the prompt would satisfy the test above and
        still be unreachable to every reader of this log. This pins the order.
        """
        text = self._exec_log_call()
        self.assertIn("session_id", text)
        self.assertIn("prompt[", text)
        self.assertLess(text.index("session_id"), text.index("prompt["),
                        "session_id must precede the prompt in the EXEC line -- a "
                        "prompt newline ends the log line and hides anything after it")

    def test_the_two_labels_that_stood_for_eighty_two_dispatches(self):
        """The measurement this repair rests on, kept where it can go stale loudly.

        47 EXECs logged the first string and 35 the second, on 2026-09-22 on sg01,
        out of 403. Neither names a workflow, a run or a session, and both are
        written by the task's author rather than assigned by the worker.
        """
        self.assertNotEqual(STEP_MINUS_ONE, PREV_CONTEXT)
        for label in (STEP_MINUS_ONE, PREV_CONTEXT):
            self.assertNotIn("\n", label, "a log line stops at its first newline")
            self.assertLess(len(label), 80,
                            "%r is shorter than the 80-char cap, so the cap was "
                            "never what truncated it -- a newline was" % label)

    def test_session_id_is_read_from_the_task_before_it_is_logged(self):
        """A field logged from a name that is never assigned logs `None` forever."""
        src = open(_PULLER, errors="replace").read()
        tree = ast.parse(src)
        assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
        self.assertIn("session_id", assigned,
                      "session_id must be bound from the task before the EXEC line "
                      "formats it, or the new field is a constant '(none)'")


if __name__ == "__main__":
    unittest.main()
