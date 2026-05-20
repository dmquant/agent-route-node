"""Antigravity (agy) CLI Hand — Google's Gemini agent via the new `agy` CLI.

The `agy` binary is Google Antigravity's CLI, slated to replace the legacy
`gemini` CLI on 2026-06-18. It is gemini-powered but ships as a native Go
binary (not npx), authenticates via OS keyring, and emits plain markdown
on stdout (no --output-format json).

This hand runs `agy --print --dangerously-skip-permissions` per task and
streams stderr in real-time for progress. Model selection happens inside
agy's stored config — there is no per-invocation --model flag.
"""

import asyncio
import os
import json
import tempfile
import time
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult, filter_noise, resolve_cli_path, get_cli_env
from app.hands.stream_processor import AgyStreamProcessor


class AgyHand(Hand):
    """Antigravity CLI agent via the `agy` binary.

    Streams stderr in real-time for progress visibility (auth, model
    selection, tool calls), and returns stdout as the final response.
    """

    name = "agy"
    hand_type = "cli"
    description = "Google Antigravity CLI (agy) — Gemini agent, successor to gemini-cli"

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        model: Optional[str] = None,
        **kwargs,
    ) -> HandResult:
        cmd = resolve_cli_path("agy")
        args = ["--print", "--dangerously-skip-permissions"]
        # agy does NOT accept --model in --print mode (the flag is unknown
        # and the binary exits non-zero). Model is set via agy's UI config.
        # If a caller passes `model=`, we record it for traceability but
        # don't forward it to the subprocess.

        # Per-request timeout — agy default is 5m. Allow override via env.
        timeout_s = os.getenv("AGY_PRINT_TIMEOUT", "")
        if timeout_s:
            args.extend(["--print-timeout", timeout_s])

        # Capture agy's internal logs to a tmpfile so they don't pollute
        # the workspace and so we can clean up after.
        log_fd, log_path = tempfile.mkstemp(prefix="agy-", suffix=".log")
        os.close(log_fd)
        args.extend(["--log-file", log_path])

        # Prompt goes last as the --print argument value.
        args.append(input)

        os.makedirs(workspace_dir, exist_ok=True)
        await self._ensure_git(workspace_dir)

        if on_log:
            short_dir = os.path.basename(workspace_dir)[:12]
            note = f"⚡ Executing with **agy** (workspace: `{short_dir}…`)"
            if model:
                note += f" — requested model `{model}` (note: agy ignores per-call model)"
            await on_log(json.dumps({"chunkType": "progress", "content": note}))

        try:
            process = await asyncio.create_subprocess_exec(
                cmd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir,
                env=get_cli_env(),
            )
        except Exception as e:
            msg = f"Failed to spawn agy: {e}"
            if on_log:
                await on_log(json.dumps({"chunkType": "error", "content": msg}))
            _safe_unlink(log_path)
            return HandResult(output=msg, exit_code=1)

        processor = AgyStreamProcessor()
        raw_stdout_chunks: list[str] = []
        raw_stderr_chunks: list[str] = []

        async def stream_stderr():
            from app.hands.base import _NOISE_PATTERNS
            from app.hands.activity_classifier import classify_line
            buf = ""
            while True:
                chunk = await process.stderr.read(256)
                if not chunk:
                    break
                text = chunk.decode('utf-8', errors='replace')
                raw_stderr_chunks.append(text)
                buf += text
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.rstrip()
                    if not line:
                        continue
                    if any(p.search(line) for p in _NOISE_PATTERNS):
                        continue
                    if on_log:
                        activity = classify_line(line, agent="gemini")
                        if activity:
                            await on_log(json.dumps(activity.to_chunk()))
                        else:
                            await on_log(json.dumps({"chunkType": "system", "content": line}))

        async def read_stdout():
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                raw_stdout_chunks.append(chunk.decode('utf-8', errors='replace'))

        try:
            await asyncio.gather(stream_stderr(), read_stdout())
            exit_code = await process.wait()
        finally:
            _safe_unlink(log_path)

        raw_stdout = ''.join(raw_stdout_chunks)
        output_text = filter_noise(raw_stdout).strip()

        # Emit the parsed final output as a single text chunk so the
        # frontend's OutputParser can render markdown / code blocks.
        if output_text and on_log:
            await on_log(json.dumps({"chunkType": "text", "content": output_text}))

        if not output_text and exit_code != 0:
            stderr_text = ''.join(raw_stderr_chunks).strip()
            if stderr_text:
                output_text = stderr_text

        return HandResult(output=output_text or f"Exit code {exit_code}", exit_code=exit_code)

    async def health_check(self) -> bool:
        path = resolve_cli_path("agy")
        return path != "agy" and os.path.isfile(path)

    async def _ensure_git(self, workspace_dir: str):
        git_dir = os.path.join(workspace_dir, '.git')
        if not os.path.exists(git_dir):
            try:
                proc = await asyncio.create_subprocess_exec(
                    'git', 'init', cwd=workspace_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
            except Exception:
                pass


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
