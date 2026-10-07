"""codex's cumulative diff echo is collapsed to one marker per block; nothing else is touched.

agora a095c28d-cbf2-4ce4-b066-095f5baac1c5 (2026-10-07): 99.1% of a 7.5 MB brief result was
`+` lines of 86 re-printed diff blocks over 8 files.
"""
from app.hands.diff_elide import elide_diffs

BLOCK = "\n".join([
    "apply patch",
    "patch: completed",
    "/ws/sessions/s1",
    "diff --git a/card-01/report.md b/card-01/report.md",
    "new file mode 100644",
    "index 0000000..c182324",
    "--- /dev/null",
    "+++ b/card-01/report.md",
    "@@ -0,0 +1,3 @@",
    "+# Report",
    "+",
    "+body line",
])
EDIT = "\n".join([
    "diff --git a/card-01/data.json b/card-01/data.json",
    "index 1111111..2222222 100644",
    "--- a/card-01/data.json",
    "+++ b/card-01/data.json",
    "@@ -1,3 +1,3 @@",
    " {",
    "-  \"a\": 1",
    "+  \"a\": 2",
    " }",
])
ANSWER = "codex\nThe brief is written to card-01/report.md.\ntokens used\n12,345"


def test_each_diff_block_becomes_one_marker_and_the_answer_survives():
    out = elide_diffs("\n".join([BLOCK, EDIT, BLOCK, EDIT, ANSWER]))
    assert out.count("[diff elided: card-01/report.md, +3/-0 lines; the file is in the workspace]") == 2
    assert out.count("[diff elided: card-01/data.json, +1/-1 lines; the file is in the workspace]") == 2
    assert "+body line" not in out and '"a": 2' not in out
    assert out.endswith(ANSWER)
    assert out.startswith("apply patch\npatch: completed\n/ws/sessions/s1\n")


def test_the_block_ends_where_the_diff_grammar_ends():
    out = elide_diffs(EDIT + "\nexec\n/bin/zsh -lc 'ls'\n+not part of a diff after a non-diff line")
    assert out.split("\n")[1:] == ["exec", "/bin/zsh -lc 'ls'", "+not part of a diff after a non-diff line"]


def test_a_diff_the_model_writes_in_a_fence_is_kept():
    fenced = "Here is the change:\n```diff\n" + EDIT + "\n```\nDone."
    assert elide_diffs(fenced) == fenced


def test_text_without_diffs_is_unchanged():
    assert elide_diffs(ANSWER) == ANSWER


def test_a_bullet_right_after_a_diff_is_not_swallowed():
    out = elide_diffs(EDIT + "\n- first finding\n- second finding")
    assert out.split("\n") == ["[diff elided: card-01/data.json, +1/-1 lines; the file is in the workspace]",
                               "- first finding", "- second finding"]


def test_a_hunk_with_a_dropped_blank_context_line_still_ends_at_prose():
    # the hand drops blank lines, so a hunk can arrive one context line short
    short = EDIT.replace("@@ -1,3 +1,3 @@", "@@ -1,4 +1,4 @@")
    out = elide_diffs(short + "\nexec\nls")
    assert out.split("\n")[1:] == ["exec", "ls"]


def test_the_codex_hand_returns_the_elided_transcript(tmp_path, monkeypatch):
    # end to end through CodexHand.execute with a fake CLI that prints the echo on stderr,
    # as codex exec does, and the answer on stdout
    import asyncio
    from app.hands import codex_hand
    fake = tmp_path / "npx"
    fake.write_text("#!/bin/sh\ncat >&2 <<'X'\n" + BLOCK + "\n" + EDIT + "\nX\nprintf 'assistant\\nTHE ANSWER\\n'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(codex_hand, "resolve_cli_path", lambda name: str(fake))
    for v in ("AI_INSTITUTE_MCP_URL", "ROS_GATEWAY_MCP_URL"):
        monkeypatch.delenv(v, raising=False)
    ws = tmp_path / "ws"
    r = asyncio.run(codex_hand.CodexHand().execute("prompt", workspace_dir=str(ws)))
    assert r.exit_code == 0
    assert "+body line" not in r.output
    assert "[diff elided: card-01/report.md, +3/-0 lines; the file is in the workspace]" in r.output
    assert "THE ANSWER" in r.output
