"""A poll failure whose message is empty must still say what failed.

Measured 2026-09-23: every one of 8,849 "Poll error:" lines in gubei12's
task_puller.log (and 7,828 on sg01) had nothing after the colon, because
httpx's timeout exceptions stringify to "". Counting the log by cause was
impossible for its single largest class.
"""
import httpx

from app.task_puller import _describe_error


def test_an_empty_message_still_names_the_type():
    assert str(httpx.ReadTimeout("")) == ""  # control: the real shape is empty
    assert _describe_error(httpx.ReadTimeout("")) == "ReadTimeout (no message)"


def test_a_message_is_kept_beside_the_type():
    assert _describe_error(ValueError("bad json")) == "ValueError: bad json"
