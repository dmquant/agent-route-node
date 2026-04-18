"""Gemini CLI Hand — Google DeepMind's Gemini agent via npx.

Real-time streaming: reads stdout/stderr line-by-line and emits
structured stream chunks via the StreamProcessor pipeline.
"""

import asyncio
import os
import json
import re
import shutil
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult, filter_noise, resolve_cli_path, get_cli_env
from app.hands.stream_processor import GeminiStreamProcessor


def _parse_gemini_json_output(raw: str) -> str:
    """Extract response text from Gemini's --output-format json output.

    Gemini CLI outputs a JSON array with response objects and stats.
    We only want the response text, not stats/metadata.
    """
    # Strategy 1: Parse as complete JSON
    try:
        data = json.loads(raw.strip())
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, dict):
                    # Skip stats/metadata objects
                    if 'stats' in item or 'totalRequests' in item or 'totalLatencyMs' in item:
                        continue
                    resp = item.get('response', item)
                    if isinstance(resp, dict):
                        text = resp.get('text', '')
                        if text:
                            parts.append(text)
                    elif isinstance(resp, str):
                        parts.append(resp)
            if parts:
                return '\n'.join(parts)
        elif isinstance(data, dict):
            # Skip stats-only objects
            if 'stats' in data and 'response' not in data:
                return ''
            resp = data.get('response', '')
            if isinstance(resp, dict):
                return resp.get('text', '')
            elif isinstance(resp, str):
                return resp
    except (json.JSONDecodeError, TypeError):
        pass

    # Strategy 2: Line-delimited JSON (each line is a JSON object)
    parts = []
    for line in raw.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
            if isinstance(chunk, dict):
                # Skip stats/metadata
                if any(k in chunk for k in ('stats', 'totalRequests', 'totalLatencyMs', 'models')):
                    continue
                text = chunk.get('text', '') or chunk.get('response', {}).get('text', '')
                if text:
                    parts.append(text)
        except (json.JSONDecodeError, TypeError):
            # Non-JSON line — only include if it looks like actual text, not raw JSON fragments
            if not line.startswith('{') and not line.startswith('['):
                parts.append(line)

    result = '\n'.join(parts) if parts else raw

    # Final cleanup: strip any JSON objects/arrays that leaked through
    result = re.sub(r'\n\s*\{[^{}]*"(?:stats|totalRequests|session_id|models)"[^{}]*\}', '', result)
    result = re.sub(r'\n\s*\{[^{}]*"(?:input|output|prompt|cached)":\s*\d+[^{}]*\}', '', result)

    return result.strip()


class GeminiHand(Hand):
    """Gemini CLI agent via `npx gemini`.

    Streams stderr in real-time for progress visibility,
    then processes the final JSON stdout for structured output.
    """

    name = "gemini"
    hand_type = "cli"
    description = "Google Gemini CLI — gemini-2.5-pro with MCP tools"

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        skills_dir = os.path.expanduser("~/.gemini/skills")
        cmd = resolve_cli_path("npx")
        args = [
            "gemini", "-p", input,
            "--output-format", "json",
            "--yolo",
            "--include-directories", skills_dir,
        ]

        os.makedirs(workspace_dir, exist_ok=True)
        await self._ensure_git(workspace_dir)

        processor = GeminiStreamProcessor()

        if on_log:
            short_dir = os.path.basename(workspace_dir)[:12]
            await on_log(json.dumps({
                "chunkType": "progress",
                "content": f"⚡ Executing with **gemini** (workspace: `{short_dir}…`)"
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
            msg = f"Failed to spawn gemini: {e}"
            if on_log:
                await on_log(json.dumps({"chunkType": "error", "content": msg}))
            return HandResult(output=msg, exit_code=1)

        # ── Stream stderr in real-time (Gemini writes progress to stderr) ──
        raw_stdout_chunks: list[str] = []

        async def stream_stderr():
            nonlocal on_log
            from app.hands.base import _NOISE_PATTERNS
            from app.hands.activity_classifier import classify_line
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
                    # Filter noise
                    if any(p.search(line) for p in _NOISE_PATTERNS):
                        continue
                    if on_log:
                        # Try to classify as a structured activity
                        activity = classify_line(line, agent="gemini")
                        if activity:
                            await on_log(json.dumps(activity.to_chunk()))
                        else:
                            # Fallback: system-level line
                            await on_log(json.dumps({"chunkType": "system", "content": line}))

        async def read_stdout():
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    break
                raw_stdout_chunks.append(chunk.decode('utf-8', errors='replace'))

        await asyncio.gather(stream_stderr(), read_stdout())
        exit_code = await process.wait()

        # ── Parse final JSON stdout ——
        raw_stdout = ''.join(raw_stdout_chunks)
        parsed = _parse_gemini_json_output(raw_stdout)
        output_text = filter_noise(parsed).strip()

        # Emit the parsed final output as a single text chunk
        # The frontend OutputParser handles markdown/code-block rendering from raw text
        if output_text and on_log:
            await on_log(json.dumps({"chunkType": "text", "content": output_text}))

        return HandResult(output=output_text or f"Exit code {exit_code}", exit_code=exit_code)

    async def health_check(self) -> bool:
        path = resolve_cli_path("npx")
        return path != "npx" and os.path.isfile(path)

    # ─── Internal helpers ─────────────

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
