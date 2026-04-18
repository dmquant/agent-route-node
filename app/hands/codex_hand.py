"""Codex CLI Hand — OpenAI's Codex agent via npx.

Real-time streaming: reads stdout line-by-line and emits
structured stream chunks via the CodexStreamProcessor.
"""

import asyncio
import os
import re
import json
import shutil
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult, filter_noise, _NOISE_PATTERNS, resolve_cli_path, get_cli_env
from app.hands.stream_processor import CodexStreamProcessor


class CodexHand(Hand):
    """Codex CLI agent via `npx codex`.

    Streams stdout in real-time with structured chunk types:
    reasoning blocks, tool calls, and prose text.
    """

    name = "codex"
    hand_type = "cli"
    description = "OpenAI Codex CLI — code generation with sandbox"

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        cmd = resolve_cli_path("npx")
        args = ["codex", "exec", "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", input]

        os.makedirs(workspace_dir, exist_ok=True)
        await self._ensure_git(workspace_dir)

        processor = CodexStreamProcessor()

        if on_log:
            short_dir = os.path.basename(workspace_dir)[:12]
            await on_log(json.dumps({
                "chunkType": "progress",
                "content": f"⚡ Executing with **codex** (workspace: `{short_dir}…`)"
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
            msg = f"Failed to spawn codex: {e}"
            if on_log:
                await on_log(json.dumps({"chunkType": "error", "content": msg}))
            return HandResult(output=msg, exit_code=1)

        # ── Stream both stdout and stderr line-by-line in real-time ──
        full_output: list[str] = []
        skip_banner = True

        async def stream_output(stream, is_stderr=False):
            nonlocal skip_banner
            buf = ""
            while True:
                chunk = await stream.read(256)
                if not chunk:
                    if buf.strip():
                        full_output.append(buf.strip())
                        if on_log:
                            for sc in processor.process_line(buf.strip()):
                                await on_log(json.dumps(sc.to_event()))
                    break
                text = chunk.decode('utf-8', errors='replace')
                buf += text
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.rstrip()
                    if not line:
                        continue
                    # Filter noise
                    if any(p.search(line) for p in _NOISE_PATTERNS):
                        continue
                    # Skip Codex banner section (user\n...\nassistant\n)
                    if skip_banner:
                        if re.match(r'^(user|assistant)$', line.strip()):
                            if line.strip() == 'assistant':
                                skip_banner = False
                            continue
                        if skip_banner and re.match(r'^[-─]{3,}$', line.strip()):
                            continue

                    full_output.append(line)
                    if on_log:
                        for sc in processor.process_line(line):
                            await on_log(json.dumps(sc.to_event()))

        await asyncio.gather(
            stream_output(process.stdout),
            stream_output(process.stderr, is_stderr=True),
        )
        exit_code = await process.wait()

        # Flush processor
        if on_log:
            for sc in processor.finalize():
                await on_log(json.dumps(sc.to_event()))

        output_text = "\n".join(full_output).strip()

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
