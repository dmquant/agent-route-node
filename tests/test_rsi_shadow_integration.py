"""Integration tests for fail-closed rsi-shadow isolation in task_puller and AgyHand."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from app.hands.agy_hand import (
    AgyHand,
    _SANDBOX_EXEC,
    _build_shadow_seatbelt_profile,
    _prepare_shadow_home,
)
from app.hands.base import Hand, HandResult
from app.rsi_shadow_guard import EXIT_REFUSED, guard


def _valid_pulled_shadow_task(hand_name="agy"):
    return {
        "id": "abcdef0123456789abcdef01",
        "hand_name": hand_name,
        "prompt": "Return JSON only.",
        "lane": "rsi-shadow",
        "rsi": {
            "lane": "rsi-shadow",
            "round_id": "round-001",
            "candidate_id": "cand-a",
            "readOnly": True,
            "credentials": "none",
            "tools": "read-only",
            "source": "rsi-shadow",
            "priority": -3,
        },
    }


class _FakeStream:
    def __init__(self, data: bytes):
        self._chunks = [data, b""] if data else [b""]

    async def read(self, _n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProcess:
    def __init__(self, stdout: bytes = b'{"ok":true}\n', stderr: bytes = b"", returncode: int = 0):
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._rc = returncode

    async def wait(self) -> int:
        return self._rc


class HandCapabilitiesTest(unittest.TestCase):
    def test_base_hand_not_shadow_capable(self):
        self.assertIs(Hand.rsi_shadow_capable, False)

    def test_other_hands_not_shadow_capable(self):
        from app.hands.claude_hand import ClaudeHand
        from app.hands.codex_hand import CodexHand
        from app.hands.gemini_hand import GeminiHand
        from app.hands.mflux_hand import MfluxHand
        from app.hands.ollama_hand import OllamaHand
        from app.hands.opencode_hand import OpencodeHand
        from app.hands.vane_hand import VaneHand
        for cls in (ClaudeHand, CodexHand, OpencodeHand, GeminiHand, OllamaHand, MfluxHand, VaneHand):
            self.assertIs(getattr(cls(), "rsi_shadow_capable", False), False, cls.__name__)

    def test_agy_hand_shadow_capable_respects_kill_env_and_split_user(self):
        h = AgyHand()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RSI_SHADOW_ENABLE_AGY", None)
            os.environ.pop("AGENT_ROUTE_HAND_USER", None)
            if sys.platform == "darwin" and os.path.isfile(_SANDBOX_EXEC):
                self.assertIs(h.rsi_shadow_capable, True)
        with mock.patch.dict(os.environ, {"RSI_SHADOW_ENABLE_AGY": "false"}):
            self.assertIs(h.rsi_shadow_capable, False)
            d = guard(_valid_pulled_shadow_task("agy"), h, {"PATH": "/usr/bin"})
            self.assertFalse(d.allowed)
        with mock.patch.dict(os.environ, {"AGENT_ROUTE_HAND_USER": "_ros_hand"}):
            self.assertIs(h.rsi_shadow_capable, False)
            d = guard(_valid_pulled_shadow_task("agy"), h, {"PATH": "/usr/bin"})
            self.assertFalse(d.allowed)


class ShadowHomeIsolationTest(unittest.TestCase):
    def test_prepare_shadow_home_copies_only_token_and_sanitizes_settings_and_mcp(self):
        with tempfile.TemporaryDirectory() as fake_home:
            cli_dir = os.path.join(fake_home, ".gemini", "antigravity-cli")
            cfg_dir = os.path.join(fake_home, ".gemini", "config")
            ssh_dir = os.path.join(fake_home, ".ssh")
            os.makedirs(cli_dir)
            os.makedirs(cfg_dir)
            os.makedirs(ssh_dir)
            with open(os.path.join(cli_dir, "antigravity-oauth-token"), "w") as f:
                f.write('{"token":{"access_token":"dummy"}}\n')
            with open(os.path.join(cli_dir, "settings.json"), "w") as f:
                json.dump({
                    "model": "Gemini 4 Hydrogen (Medium)",
                    "effort": "medium",
                    "permissions": {"allow": ["command(rm)"]},
                    "trustedWorkspaces": ["/Users/daoming"],
                }, f)
            with open(os.path.join(cfg_dir, "mcp_config.json"), "w") as f:
                json.dump({"mcpServers": {"ai-institute": {"headers": {"X-Admin-Token": "secret"}}}}, f)
            with open(os.path.join(ssh_dir, "id_ed25519"), "w") as f:
                f.write("PRIVATE KEY")

            sh = _prepare_shadow_home(fake_home)
            try:
                self.assertEqual(os.stat(sh).st_mode & 0o777, 0o700)
                self.assertTrue(os.path.isfile(os.path.join(sh, ".gemini", "antigravity-cli", "antigravity-oauth-token")))
                self.assertFalse(os.path.exists(os.path.join(sh, ".ssh")))
                with open(os.path.join(sh, ".gemini", "antigravity-cli", "settings.json")) as f:
                    sanitized = json.load(f)
                self.assertEqual(sanitized, {"model": "Gemini 4 Hydrogen (Medium)", "effort": "medium"})
                with open(os.path.join(sh, ".gemini", "config", "mcp_config.json")) as f:
                    mcp = json.load(f)
                self.assertEqual(mcp, {"mcpServers": {}})
            finally:
                import shutil
                shutil.rmtree(sh, ignore_errors=True)

    def test_prepare_shadow_home_fails_closed_without_token(self):
        with tempfile.TemporaryDirectory() as empty_home:
            with self.assertRaises(RuntimeError):
                _prepare_shadow_home(empty_home)

    @unittest.skipUnless(sys.platform == "darwin" and os.path.isfile(_SANDBOX_EXEC), "macOS sandbox-exec only")
    def test_seatbelt_profile_blocks_secret_read_and_home_write_at_kernel(self):
        with tempfile.TemporaryDirectory() as fake_home:
            ar_dir = os.path.join(fake_home, ".agent-route")
            docs_dir = os.path.join(fake_home, "Documents")
            os.makedirs(ar_dir)
            os.makedirs(docs_dir)
            secret_path = os.path.join(ar_dir, ".env")
            doc_path = os.path.join(docs_dir, "private.txt")
            with open(secret_path, "w") as f:
                f.write("NODE_KEY=canary-secret-12345\n")
            with open(doc_path, "w") as f:
                f.write("PRIVATE_DOC=canary-doc-67890\n")
            profile = _build_shadow_seatbelt_profile(fake_home)
            write_target = os.path.join(fake_home, "should_not_write.txt")
            r = subprocess.run(
                [
                    _SANDBOX_EXEC, "-p", profile,
                    ("/usr/bin/python3" if os.path.isfile("/usr/bin/python3") else sys.executable), "-c",
                    (
                        "import sys\n"
                        "out = []\n"
                        "for p in sys.argv[1:3]:\n"
                        "  try:\n    open(p).read(); out.append('READ_OK')\n"
                        "  except PermissionError:\n    out.append('READ_EPERM')\n"
                        "try:\n  open(sys.argv[3], 'w').write('x'); out.append('WRITE_OK')\n"
                        "except PermissionError:\n  out.append('WRITE_EPERM')\n"
                        "print(','.join(out))\n"
                    ),
                    secret_path,
                    doc_path,
                    write_target,
                ],
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "READ_EPERM,READ_EPERM,WRITE_EPERM")
            self.assertFalse(os.path.exists(write_target))


class AgyHandShadowExecutionTest(unittest.TestCase):
    def test_agy_shadow_strips_creds_uses_plan_mode_skips_mcp_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as ws:
            cli_dir = os.path.join(fake_home, ".gemini", "antigravity-cli")
            os.makedirs(cli_dir)
            with open(os.path.join(cli_dir, "antigravity-oauth-token"), "w") as f:
                f.write('{"token":{"access_token":"ya29.fake-access-token-secret-999","refresh_token":"1//fake-refresh-token-secret-888","auth":"opaque-auth-blob-77777","token_type":"Bearer"}}\n')

            dirty_env = {
                "PATH": "/usr/bin:/bin",
                "LANG": "en_US.UTF-8",
                "AI_INSTITUTE_MCP_KEY": "super-secret-institute-key",
                "NODE_KEY": "super-secret-node-key",
                "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-secret",
                "OPENAI_API_KEY": "sk-openai-secret",
                "CF_WORKER_API_KEY": "cf-secret",
            }
            hand = AgyHand()
            decision = guard(_valid_pulled_shadow_task("agy"), hand, dirty_env)
            self.assertTrue(decision.allowed and decision.applies)

            captured = {}

            async def fake_spawn(name, cmd, args, **kw):
                captured["cmd"] = cmd
                captured["args"] = list(args)
                captured["env"] = dict(kw["env"])
                captured["shadow_home_existed_during_run"] = os.path.isdir(kw["env"]["HOME"])
                with open(os.path.join(kw["env"]["HOME"], ".gemini", "config", "mcp_config.json")) as mf:
                    captured["mcp_cfg_during_run"] = json.load(mf)
                return _FakeProcess(
                    stdout=b'{"thesis":"ok","echo":"ya29.fake-access-token-secret-999","ref":"1//fake-refresh-token-secret-888","a":"opaque-auth-blob-77777"}\n'
                )

            with mock.patch("app.hands.agy_hand.os.path.expanduser", return_value=fake_home), \
                 mock.patch("app.hands.agy_hand.mcp_prepare") as mock_mcp, \
                 mock.patch("app.hands.agy_hand.resolve_cli_path", return_value="/opt/ros-runtime/bin/agy"), \
                 mock.patch("app.hands.agy_hand.spawn_hand", side_effect=fake_spawn):
                res = asyncio.run(hand.execute("hello shadow", workspace_dir=ws, rsi_shadow=decision))

            self.assertEqual(res.exit_code, 0)
            self.assertNotIn("ya29.fake-access-token-secret-999", res.output)
            self.assertNotIn("1//fake-refresh-token-secret-888", res.output)
            self.assertNotIn("opaque-auth-blob-77777", res.output)
            self.assertIn("[REDACTED_AGY_TOKEN]", res.output)
            mock_mcp.assert_not_called()
            self.assertEqual(captured["cmd"], _SANDBOX_EXEC)
            self.assertEqual(captured["args"][0], "-p")
            self.assertIn('(deny file-read* (subpath "/Users"))', captured["args"][1])
            self.assertEqual(captured["args"][2], "/opt/ros-runtime/bin/agy")
            self.assertIn("--mode", captured["args"])
            self.assertEqual(captured["args"][captured["args"].index("--mode") + 1], "plan")
            self.assertNotIn("--dangerously-skip-permissions", captured["args"])
            for secret_k in ("AI_INSTITUTE_MCP_KEY", "NODE_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CF_WORKER_API_KEY"):
                self.assertNotIn(secret_k, captured["env"])
            self.assertTrue(captured["shadow_home_existed_during_run"])
            self.assertEqual(captured["mcp_cfg_during_run"], {"mcpServers": {}})
            self.assertNotEqual(captured["env"]["HOME"], fake_home)
            self.assertFalse(os.path.exists(captured["env"]["HOME"]))

    def test_agy_shadow_redacts_token_in_geo_block_excerpt_path(self):
        with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as ws:
            cli_dir = os.path.join(fake_home, ".gemini", "antigravity-cli")
            os.makedirs(cli_dir)
            with open(os.path.join(cli_dir, "antigravity-oauth-token"), "w") as f:
                f.write('{"credential":"secret-geo-leak-99887766"}\n')

            hand = AgyHand()
            decision = guard(_valid_pulled_shadow_task("agy"), hand, {"PATH": "/usr/bin"})

            async def fake_spawn(name, cmd, args, **kw):
                log_path = args[args.index("--log-file") + 1]
                with open(log_path, "w") as lf:
                    lf.write("User location is not supported for the API use: secret-geo-leak-99887766\n")
                return _FakeProcess(stdout=b"", stderr=b"", returncode=0)

            with mock.patch("app.hands.agy_hand.os.path.expanduser", return_value=fake_home), \
                 mock.patch("app.hands.agy_hand.resolve_cli_path", return_value="/opt/ros-runtime/bin/agy"), \
                 mock.patch("app.hands.agy_hand.spawn_hand", side_effect=fake_spawn):
                res = asyncio.run(hand.execute("hello shadow", workspace_dir=ws, rsi_shadow=decision))

            self.assertNotEqual(res.exit_code, 0)
            self.assertNotIn("secret-geo-leak-99887766", res.output)
            self.assertIn("[REDACTED_AGY_TOKEN]", res.output)

    def test_agy_shadow_refuses_malformed_decision(self):
        hand = AgyHand()
        bad = types.SimpleNamespace(allowed=False, applies=True, env={})
        res = asyncio.run(hand.execute("hello", rsi_shadow=bad))
        self.assertEqual(res.exit_code, EXIT_REFUSED)


class TaskPullerShadowIntegrationTest(unittest.TestCase):
    def test_non_capable_hand_refused_with_exit_66_and_no_workspace(self):
        from app import task_puller
        from app.hands.registry import hand_registry

        posted, ran = [], []

        class FakeClaude:
            name = "claude"
            rsi_shadow_capable = False

            async def execute(self, *a, **k):
                ran.append(1)
                return HandResult(output="nope", exit_code=0)

        async def fake_post(client, url, key, tid, code, text, metrics=None):
            posted.append((code, text, metrics))

        task = _valid_pulled_shadow_task("claude")
        task_puller._inflight_ids.add(task["id"])
        with tempfile.TemporaryDirectory() as ws_root, \
             mock.patch.object(hand_registry, "resolve", return_value=(FakeClaude(), ["claude"])), \
             mock.patch.object(task_puller, "_post_result", side_effect=fake_post), \
             mock.patch("app.config.get_workspaces_dir", return_value=ws_root):
            asyncio.run(task_puller._execute_one(task, "http://x", "k"))

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], EXIT_REFUSED)
        self.assertIn("RSI_SHADOW_REFUSED", posted[0][1])
        self.assertEqual(ran, [])

    def test_null_top_level_lane_with_meta_lane_refused_even_on_agy(self):
        from app import task_puller
        from app.hands.registry import hand_registry

        posted, ran = [], []

        class FakeAgy:
            name = "agy"
            rsi_shadow_capable = True

            async def execute(self, *a, **k):
                ran.append(1)
                return HandResult(output="nope", exit_code=0)

        async def fake_post(client, url, key, tid, code, text, metrics=None):
            posted.append((code, text))

        task = _valid_pulled_shadow_task("agy")
        task["lane"] = None
        task["meta"] = {"lane": "rsi-shadow"}
        task_puller._inflight_ids.add(task["id"])
        with mock.patch.object(hand_registry, "resolve", return_value=(FakeAgy(), ["agy"])), \
             mock.patch.object(task_puller, "_post_result", side_effect=fake_post):
            asyncio.run(task_puller._execute_one(task, "http://x", "k"))

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], EXIT_REFUSED)
        self.assertEqual(ran, [])

    def test_valid_shadow_task_skips_r2_sync_harvester_and_sweeper_and_cleans_workspace(self):
        from app import task_puller
        from app.hands.registry import hand_registry

        posted = []
        seen = {}

        class FakeAgy:
            name = "agy"
            rsi_shadow_capable = True

            async def execute(self, prompt, **kw):
                seen["workspace_dir"] = kw["workspace_dir"]
                seen["rsi_shadow"] = kw.get("rsi_shadow")
                seen["ws_exists_during_run"] = os.path.isdir(kw["workspace_dir"])
                return HandResult(output='{"shadow":true}', exit_code=0)

        async def fake_post(client, url, key, tid, code, text, metrics=None):
            posted.append((code, text, metrics))

        task = _valid_pulled_shadow_task("agy")
        task_puller._inflight_ids.add(task["id"])
        with mock.patch.object(hand_registry, "resolve", return_value=(FakeAgy(), ["agy"])), \
             mock.patch.object(task_puller, "_post_result", side_effect=fake_post), \
             mock.patch.object(task_puller, "_sync_workspace_from_r2") as mock_r2_pull, \
             mock.patch("app.workspace_sync.sync_session_now") as mock_r2_push, \
             mock.patch("app.url_harvester.harvest_urls_for_task") as mock_harvest, \
             mock.patch("app.agy_sweeper.sweep_after_agy_task") as mock_sweep:
            asyncio.run(task_puller._execute_one(task, "http://x", "k"))

        mock_r2_pull.assert_not_called()
        mock_r2_push.assert_not_called()
        mock_harvest.assert_not_called()
        mock_sweep.assert_not_called()
        self.assertTrue(seen["ws_exists_during_run"])
        self.assertFalse(os.path.exists(seen["workspace_dir"]))
        self.assertTrue(seen["rsi_shadow"].allowed and seen["rsi_shadow"].applies)
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0][0], 0)
        self.assertEqual(posted[0][1], '{"shadow":true}')
        self.assertEqual(posted[0][2].get("lane"), "rsi-shadow")


if __name__ == "__main__":
    unittest.main()
