"""OpenCode Hand — execute prompts via the `opencode run` CLI.

Mirrors the per-task subprocess pattern used by claude_hand / gemini_hand /
codex_hand: each task spawns a fresh `opencode run` process with
`cwd=workspace_dir`. opencode's tool-use (file reads/writes/searches)
operates inside that directory by default, so per-session file isolation
and cross-node continuity (via the puller's R2 sync) come for free.

Output format
─────────────
opencode run --format json emits a stream of events:
  {"type":"step_start", "part":{...}}
  {"type":"text", "part":{"type":"text", "text":"..."}}
  {"type":"tool_use", "part":{"type":"tool", "tool":"write",
                              "state":{"input":{...}, "output":"..."}}}
  {"type":"step_finish", "part":{"tokens":{...}, "cost":0.0}}

Rather than returning the raw event stream as the task output, we render
the events into a clean markdown structure:

    <assistant text, joined from all `text` events>

    ---

    **Wrote:** `HELLO.md`
    **Ran:** 2 bash commands
    *tokens: 9,757 (in 9,746, out 11) • cost: $0.0000 • duration: 1.5s*

Sections that have no content are omitted. If parsing fails (e.g.
--format json wasn't honored) we fall back to raw stdout.

Authentication: opencode's CLI uses whatever credentials `opencode auth
set` has saved (~/.opencode/...). Configure that on the edge node — same
as `claude`, `gemini`, `codex` CLIs.

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
from collections import Counter
from typing import Optional, Callable, Any

from app.hands.base import (
    Hand, HandResult, _NOISE_PATTERNS, resolve_cli_path, get_cli_env,
)


class _OpencodeEvents:
    """Accumulates structured info from the opencode JSON event stream."""

    def __init__(self) -> None:
        self.text_parts: list[str] = []
        self.tool_calls: list[dict] = []
        self.files_written: list[str] = []
        self.files_edited: list[str] = []
        self.files_read: list[str] = []
        self.bash_commands: list[str] = []
        self.tokens_input = 0
        self.tokens_output = 0
        self.tokens_total = 0
        self.cost = 0.0
        self.start_ts: Optional[int] = None
        self.end_ts: Optional[int] = None
        self.session_id: Optional[str] = None
        self.errors: list[str] = []

    def absorb(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        et = event.get("type", "")
        part = event.get("part", {}) if isinstance(event.get("part"), dict) else {}

        # Track session id + timestamps for the footer
        sid = event.get("sessionID") or part.get("sessionID")
        if sid and not self.session_id:
            self.session_id = sid
        ts = event.get("timestamp")
        if isinstance(ts, (int, float)):
            if self.start_ts is None:
                self.start_ts = int(ts)
            self.end_ts = int(ts)

        # ── Text from the assistant ──
        # Outer event type is "text" (or sometimes inner part.type is "text")
        if et == "text" or part.get("type") == "text":
            text = part.get("text", "")
            if isinstance(text, str) and text:
                self.text_parts.append(text)
            return

        # ── Tool calls ──
        if et == "tool_use" or part.get("type") == "tool":
            tool = part.get("tool") or ""
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
            tool_output = state.get("output", "")
            status = state.get("status", "")

            self.tool_calls.append({
                "tool": tool,
                "input": tool_input,
                "output": tool_output if isinstance(tool_output, str) else "",
                "status": status,
            })

            # Specialise the common tools so the rendered output is useful
            file_path = (tool_input.get("filePath")
                         or tool_input.get("path")
                         or tool_input.get("file_path"))
            if tool == "write" and file_path:
                self.files_written.append(file_path)
            elif tool == "edit" and file_path:
                self.files_edited.append(file_path)
            elif tool == "read" and file_path:
                self.files_read.append(file_path)
            elif tool == "bash":
                cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
                if cmd:
                    self.bash_commands.append(cmd)
            if status == "error" and isinstance(tool_output, str) and tool_output:
                self.errors.append(f"{tool}: {tool_output[:200]}")
            return

        # ── Step / cost / token accounting ──
        if et == "step_finish":
            tokens = part.get("tokens", {}) if isinstance(part.get("tokens"), dict) else {}
            self.tokens_input += int(tokens.get("input") or 0)
            self.tokens_output += int(tokens.get("output") or 0)
            self.tokens_total += int(tokens.get("total") or 0)
            cost = part.get("cost")
            if isinstance(cost, (int, float)):
                self.cost += float(cost)
            return

        # ── Errors at the event level ──
        if et in ("error", "tool_error"):
            err = (event.get("error") or part.get("error") or "")
            if isinstance(err, str) and err:
                self.errors.append(err[:300])

    def render(self) -> str:
        """Build the final structured output string."""
        sections: list[str] = []

        text = "".join(self.text_parts).strip()
        if text:
            sections.append(text)

        ops_lines: list[str] = []
        if self.files_written:
            uniq = list(dict.fromkeys(self.files_written))
            ops_lines.append(f"**Wrote:** {', '.join(f'`{p}`' for p in uniq)}")
        if self.files_edited:
            uniq = list(dict.fromkeys(self.files_edited))
            ops_lines.append(f"**Edited:** {', '.join(f'`{p}`' for p in uniq)}")
        if self.files_read:
            uniq = list(dict.fromkeys(self.files_read))
            shown = ', '.join(f'`{p}`' for p in uniq[:5])
            more = f' (+{len(uniq) - 5} more)' if len(uniq) > 5 else ''
            ops_lines.append(f"**Read:** {shown}{more}")
        if self.bash_commands:
            n = len(self.bash_commands)
            ops_lines.append(f"**Ran:** {n} bash command{'s' if n != 1 else ''}")
        # Catch-all for other tool types
        other = [tc["tool"] for tc in self.tool_calls
                 if tc["tool"] not in ("write", "edit", "read", "bash")]
        if other:
            counts = Counter(other)
            tools_str = ', '.join(f"{t} ×{n}" for t, n in counts.items())
            ops_lines.append(f"**Tools:** {tools_str}")
        if self.errors:
            for err in self.errors[:3]:
                ops_lines.append(f"⚠️ {err}")

        # Usage footer
        meta_parts: list[str] = []
        if self.tokens_total:
            meta_parts.append(
                f"tokens: {self.tokens_total:,} "
                f"(in {self.tokens_input:,}, out {self.tokens_output:,})"
            )
        if self.cost:
            meta_parts.append(f"cost: ${self.cost:.4f}")
        if self.start_ts and self.end_ts and self.end_ts > self.start_ts:
            dur = (self.end_ts - self.start_ts) / 1000
            meta_parts.append(f"duration: {dur:.1f}s")

        footer: list[str] = []
        if ops_lines:
            footer.extend(ops_lines)
        if meta_parts:
            footer.append("*" + " • ".join(meta_parts) + "*")

        if footer:
            sections.append("\n".join(footer))

        if len(sections) > 1:
            return "\n\n---\n\n".join(sections)
        return sections[0] if sections else ""


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

        events = _OpencodeEvents()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def stream_stdout() -> None:
            buf = ""
            assert process.stdout is not None
            while True:
                chunk = await process.stdout.read(2048)
                if not chunk:
                    if buf.strip():
                        stdout_lines.append(buf)
                        try:
                            events.absorb(json.loads(buf))
                        except Exception:
                            pass
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip()
                    if not line:
                        continue
                    stdout_lines.append(line)
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue
                    events.absorb(evt)
                    if on_log:
                        # Forward a friendlier per-event preview to the live UI
                        et = evt.get("type", "?")
                        part = evt.get("part", {}) if isinstance(evt.get("part"), dict) else {}
                        if et == "text":
                            preview = (part.get("text") or "")[:120]
                            await on_log(json.dumps({"chunkType": "text", "content": preview}))
                        elif et == "tool_use":
                            tool = part.get("tool", "?")
                            ti = part.get("state", {}).get("input", {}) if isinstance(part.get("state"), dict) else {}
                            target = ti.get("filePath") or ti.get("command") or ""
                            await on_log(json.dumps({
                                "chunkType": "system",
                                "content": f"opencode tool: {tool} {target[:80]}",
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

        # Build the final structured output. If events.render() comes back
        # empty (--format json wasn't honored / parser drift), fall back to
        # a trimmed dump of stdout so the consumer still gets something.
        output_text = events.render()
        if not output_text:
            raw = "\n".join(stdout_lines).strip()
            output_text = raw or "\n".join(stderr_lines).strip()
        if exit_code != 0 and not output_text:
            output_text = f"opencode exited with code {exit_code}"

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
