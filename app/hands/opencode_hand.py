"""OpenCode Hand — execute prompts via the `opencode run` CLI.

Mirrors the per-task subprocess pattern used by claude_hand / gemini_hand /
codex_hand: each task spawns a fresh `opencode run` process with
`cwd=workspace_dir`. opencode's tool-use (file reads/writes/searches)
operates inside that directory by default, so per-session file isolation
and cross-node continuity (via the puller's R2 sync) come for free.

Earlier we shipped an HTTP server-mode variant of this hand. It worked but
shared a single cwd across all tasks — opencode runs in `cwd` it was
started with, regardless of which agent-route session is calling. That
broke session-scoped file persistence. The CLI subprocess approach
restores parity with other CLI hands.

Authentication: opencode's CLI uses whatever credentials `opencode auth
set` has saved (~/.opencode/...). Make sure that's configured on the
edge node — same as `claude`, `gemini`, `codex` CLIs.

Configurable via env on the edge node:
  OPENCODE_MODEL        — provider/model id, e.g. 'anthropic/claude-3-5-sonnet'
                          (omit to use opencode's configured default)
  OPENCODE_AGENT        — optional agent name to scope tool access
  OPENCODE_TIMEOUT_S    — subprocess wall timeout (default: 600)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional, Callable, Any

from app.hands.base import (
    Hand, HandResult, _NOISE_PATTERNS, resolve_cli_path, get_cli_env,
)


class OpencodeHand(Hand):
    name = "opencode"
    hand_type = "cli"
    description = "OpenCode CLI — multi-model coding agent via `opencode run`"

    def __init__(self):
        self.default_model = os.getenv("OPENCODE_MODEL", "")
        self.default_agent = os.getenv("OPENCODE_AGENT", "")
        self.timeout_s = int(os.getenv("OPENCODE_TIMEOUT_S", "600"))

    async def execute(
        self,
        input: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Any]] = None,
        **kwargs,
    ) -> HandResult:
        cmd = resolve_cli_path("opencode")
        if cmd == "opencode" and not os.path.isfile(cmd):
            return HandResult(
                output="opencode CLI not found on PATH — install via https://opencode.ai/install or set NODE_BIN_DIR",
                exit_code=1,
            )

        model = kwargs.get("model") or self.default_model
        agent = kwargs.get("agent") or self.default_agent

        # `opencode run [message..]` — flags before, prompt as last positional.
        # `--format json` gives us parseable events; `--dangerously-skip-permissions`
        # avoids interactive prompts since this is non-interactive.
        args: list[str] = ["run", "--format", "json", "--dangerously-skip-permissions"]
        if model:
            args += ["--model", model]
        if agent:
            args += ["--agent", agent]
        args.append(input)

        os.makedirs(workspace_dir, exist_ok=True)

        if on_log:
            short_dir = os.path.basename(workspace_dir)[:12]
            await on_log(json.dumps({
                "chunkType": "progress",
                "content": f"⚡ Executing with **opencode** "
                           f"(workspace: `{short_dir}…`, model: {model or 'default'})",
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
            msg = f"Failed to spawn opencode: {e}"
            if on_log:
                await on_log(json.dumps({"chunkType": "error", "content": msg}))
            return HandResult(output=msg, exit_code=1)

        # ── Stream stdout / stderr concurrently ──
        # opencode --format json emits one JSON event per line. We collect
        # the raw stream and assemble the assistant's text from any event
        # that carries a `text` payload. Tolerant to schema variations.
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        text_chunks: list[str] = []
        seen_message_text: dict[str, str] = {}

        def _absorb_event(line: str) -> None:
            """Parse one stdout line as JSON, harvest any text content."""
            try:
                evt = json.loads(line)
            except Exception:
                return
            # opencode events vary by version. Cover the common shapes:
            #   { "type": "text_delta", "text": "..." }
            #   { "type": "message", "parts": [{"type":"text","text":"..."}] }
            #   { "type": "message_delta", "delta": { "text": "..." } }
            #   { "type": "assistant", "message": { "parts": [...] } }
            t = evt.get("type", "")
            if t in ("text_delta", "delta") and isinstance(evt.get("text"), str):
                text_chunks.append(evt["text"])
                return
            delta = evt.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("text"), str):
                text_chunks.append(delta["text"])
                return
            # Full-message shapes — dedup by message id so a final summary
            # event doesn't double-count text we already streamed via deltas.
            for container_key in ("message", "info"):
                msg = evt.get(container_key)
                if isinstance(msg, dict):
                    msg_id = msg.get("id") or msg.get("messageID") or ""
                    parts = msg.get("parts")
                    if isinstance(parts, list):
                        full = "".join(
                            p.get("text", "")
                            for p in parts
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                        if msg_id and full and seen_message_text.get(msg_id) != full:
                            # If we already accumulated streamed deltas,
                            # prefer the consolidated full text — clear the
                            # deltas so we don't double up.
                            seen_message_text[msg_id] = full
                            text_chunks.clear()
                            text_chunks.append(full)
                        elif not msg_id and full and not text_chunks:
                            text_chunks.append(full)
                        return
            # Fallback: any 'text' field at the top level
            if isinstance(evt.get("text"), str) and evt["text"]:
                text_chunks.append(evt["text"])

        async def stream_stdout() -> None:
            buf = ""
            assert process.stdout is not None
            while True:
                chunk = await process.stdout.read(2048)
                if not chunk:
                    if buf.strip():
                        stdout_lines.append(buf)
                        _absorb_event(buf)
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip()
                    if not line:
                        continue
                    stdout_lines.append(line)
                    _absorb_event(line)
                    if on_log:
                        # Forward a brief preview so the UI sees activity
                        try:
                            evt = json.loads(line)
                            etype = evt.get("type", "")
                        except Exception:
                            etype = "raw"
                        await on_log(json.dumps({
                            "chunkType": "system",
                            "content": f"opencode: {etype}",
                        }))

        async def stream_stderr() -> None:
            buf = ""
            assert process.stderr is not None
            while True:
                chunk = await process.stderr.read(2048)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip()
                    if not line:
                        continue
                    if any(p.search(line) for p in _NOISE_PATTERNS):
                        continue
                    stderr_lines.append(line)
                    if on_log:
                        await on_log(json.dumps({
                            "chunkType": "error" if "error" in line.lower() else "system",
                            "content": line,
                        }))

        try:
            await asyncio.wait_for(
                asyncio.gather(stream_stdout(), stream_stderr()),
                timeout=self.timeout_s,
            )
            exit_code = await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return HandResult(
                output=f"opencode timed out after {self.timeout_s}s",
                exit_code=124,
            )

        # Build final output. Prefer assembled text; fall back to raw stdout
        # if --format json wasn't honored (older opencode? user override?).
        output_text = "".join(text_chunks).strip()
        if not output_text:
            output_text = "\n".join(stdout_lines).strip()
        if exit_code != 0 and not output_text:
            err = "\n".join(stderr_lines).strip()
            output_text = err or f"opencode exited with code {exit_code}"

        return HandResult(output=output_text or f"Exit code {exit_code}", exit_code=exit_code)

    async def health_check(self) -> bool:
        path = resolve_cli_path("opencode")
        return path != "opencode" and os.path.isfile(path) and os.access(path, os.X_OK)

    def info(self) -> dict:
        return {
            "name": self.name,
            "type": self.hand_type,
            "description": self.description,
            "model": self.default_model or "(opencode default)",
        }
