"""Claude Code Hand — Anthropic's Claude Code agent via npx.

Real-time streaming: reads stdout line-by-line and emits
structured stream chunks via the ClaudeStreamProcessor.
"""

import asyncio
import os
import re
import json
import shutil
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult, filter_noise, _NOISE_PATTERNS, resolve_cli_path, get_cli_env
from app.hands.stream_processor import ClaudeStreamProcessor
from app.hands.activity_classifier import classify_line


class ClaudeHand(Hand):
    """Claude Code CLI agent via `npx @anthropic-ai/claude-code`.

    Streams stdout in real-time with structured chunk types:
    thinking blocks, tool calls, code writes, and prose text.
    """

    name = "claude"
    hand_type = "cli"
    description = "Anthropic Claude Code — claude-sonnet-4 with long context"

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        cmd = resolve_cli_path("npx")
        args = ["@anthropic-ai/claude-code", "-p", "--dangerously-skip-permissions", input]

        os.makedirs(workspace_dir, exist_ok=True)
        await self._ensure_git(workspace_dir)

        processor = ClaudeStreamProcessor()

        if on_log:
            short_dir = os.path.basename(workspace_dir)[:12]
            await on_log(json.dumps({
                "chunkType": "progress",
                "content": f"⚡ Executing with **claude** (workspace: `{short_dir}…`)"
            }))

        try:
            process = await asyncio.create_subprocess_exec(
                cmd, *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace_dir,
                env=get_cli_env(),
            )
        except Exception as e:
            msg = f"Failed to spawn claude: {e}"
            if on_log:
                await on_log(json.dumps({"chunkType": "error", "content": msg}))
            return HandResult(output=msg, exit_code=1)

        # ── Stream stdout line-by-line in real-time ──
        full_output: list[str] = []

        async def stream_stdout():
            buf = ""
            while True:
                chunk = await process.stdout.read(256)
                if not chunk:
                    # Flush remaining buffer
                    if buf.strip():
                        full_output.append(buf)
                        if on_log:
                            for sc in processor.process_line(buf):
                                await on_log(json.dumps(sc.to_event()))
                    break
                text = chunk.decode('utf-8', errors='replace')
                buf += text
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line_stripped = line.rstrip()
                    if not line_stripped:
                        continue
                    full_output.append(line_stripped)
                    if on_log:
                        # Try activity classification first (catches tool uses)
                        activity = classify_line(line_stripped, agent="claude")
                        if activity:
                            await on_log(json.dumps(activity.to_chunk()))
                        else:
                            for sc in processor.process_line(line_stripped):
                                await on_log(json.dumps(sc.to_event()))

        async def stream_stderr():
            """Stream stderr for error detection."""
            buf = ""
            while True:
                chunk = await process.stderr.read(256)
                if not chunk:
                    break
                text = chunk.decode('utf-8', errors='replace')
                buf += text
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.rstrip()
                    if not line:
                        continue
                    if any(p.search(line) for p in _NOISE_PATTERNS):
                        continue
                    # Stderr lines are system/error type
                    if on_log:
                        await on_log(json.dumps({"chunkType": "system", "content": line}))

        await asyncio.gather(stream_stdout(), stream_stderr())
        exit_code = await process.wait()

        # Flush processor
        if on_log:
            for sc in processor.finalize():
                await on_log(json.dumps(sc.to_event()))

        # Build final output
        output_text = "\n".join(full_output).strip()

        # Claude-specific: if stdout is empty, check stderr for error
        if exit_code != 0 and not output_text:
            output_text = f"Process exited with code {exit_code}"

        return HandResult(output=output_text or f"Exit code {exit_code}", exit_code=exit_code)

    async def health_check(self) -> bool:
        path = resolve_cli_path("npx")
        return path != "npx" and os.path.isfile(path)

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
