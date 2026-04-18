"""Stream Processors — per-hand output structuring for real-time display.

Each agent CLI has a distinct output format. Stream processors parse raw
stdout/stderr lines and annotate them with semantic types so the frontend
can render them differently:

  - text:       Normal response prose
  - thinking:   Internal reasoning (Claude's thinking, Gemini's planning)
  - tool_use:   Tool call invocation (file edit, command run, etc.)
  - tool_result: Tool call output/result
  - code_write: File creation/modification
  - code_block: Fenced code block content
  - progress:   Status/progress indicator
  - error:      Error message
  - system:     System/framework message (noise, filtered but passed)
"""

import re
import json
from typing import Optional
from dataclasses import dataclass


@dataclass
class StreamChunk:
    """A typed chunk of agent output for structured rendering."""
    type: str       # text, thinking, tool_use, tool_result, code_write, code_block, progress, error, system
    content: str
    metadata: Optional[dict] = None

    def to_event(self) -> dict:
        """Convert to WebSocket event payload."""
        d = {"chunkType": self.type, "content": self.content}
        if self.metadata:
            d["meta"] = self.metadata
        return d


class BaseStreamProcessor:
    """Base stream processor — passes raw text with minimal annotation."""

    agent_name = "unknown"

    def __init__(self):
        self._in_code_block = False
        self._code_lang = ""
        self._code_lines: list = []

    def process_line(self, line: str) -> list[StreamChunk]:
        """Process a single line and return typed chunks."""

        # ── Code block detection ──
        stripped = line.strip()

        if stripped.startswith("```") and not self._in_code_block:
            self._in_code_block = True
            self._code_lang = stripped[3:].strip() or "text"
            self._code_lines = []
            return []  # Don't emit opening fence

        if stripped.startswith("```") and self._in_code_block:
            self._in_code_block = False
            block = "\n".join(self._code_lines)
            return [StreamChunk("code_block", block, {"lang": self._code_lang})]

        if self._in_code_block:
            self._code_lines.append(line)
            return []  # Accumulate until closing fence

        # ── Error detection ──
        if re.search(r'(?:❌|Error:|error:|FAILED|fatal:|panic:)', line, re.IGNORECASE):
            return [StreamChunk("error", line)]

        # ── Progress indicators ──
        if re.search(r'(?:⚡|🔄|⏳|▶|→|\.{3}$)', line):
            return [StreamChunk("progress", line)]

        # ── Default: text ──
        if stripped:
            return [StreamChunk("text", line)]

        return []  # Skip blank lines during streaming

    def finalize(self) -> list[StreamChunk]:
        """Flush any buffered state."""
        chunks = []
        if self._in_code_block and self._code_lines:
            chunks.append(StreamChunk("code_block", "\n".join(self._code_lines), {"lang": self._code_lang}))
            self._in_code_block = False
        return chunks


class GeminiStreamProcessor(BaseStreamProcessor):
    """Gemini CLI processor — handles JSON output and tool calls."""

    agent_name = "gemini"

    def __init__(self):
        super().__init__()
        self._json_buffer: list = []
        self._in_json = False

    def process_line(self, line: str) -> list[StreamChunk]:
        stripped = line.strip()

        # ── Detect JSON output (Gemini --output-format json) ──
        if stripped.startswith("{") or stripped.startswith("["):
            self._in_json = True
            self._json_buffer = [stripped]
            return self._try_parse_json()

        if self._in_json:
            self._json_buffer.append(stripped)
            return self._try_parse_json()

        # ── Tool call patterns ──
        if re.match(r'(?:Reading|Writing|Creating|Editing|Deleting)\s+(?:file|directory)', line, re.IGNORECASE):
            return [StreamChunk("tool_use", line, {"tool": "file_op"})]

        if re.match(r'(?:Running|Executing)\s+(?:command|shell|bash)', line, re.IGNORECASE):
            return [StreamChunk("tool_use", line, {"tool": "shell"})]

        if re.match(r'(?:Searching|Grep|Find)', line, re.IGNORECASE):
            return [StreamChunk("tool_use", line, {"tool": "search"})]

        # ── File write detection ──
        if re.match(r'^(?:Created|Modified|Updated|Wrote)\s+.*\.\w+', line):
            filename = re.search(r'[\w/\.\-]+\.\w+', line)
            return [StreamChunk("code_write", line, {"file": filename.group() if filename else ""})]

        return super().process_line(line)

    def _try_parse_json(self) -> list[StreamChunk]:
        """Try to parse accumulated JSON and extract structured response."""
        raw = "\n".join(self._json_buffer)
        try:
            data = json.loads(raw)
            self._in_json = False
            self._json_buffer = []

            # Extract response text from Gemini JSON
            if isinstance(data, list):
                parts = []
                for item in data:
                    if isinstance(item, dict):
                        resp = item.get("response", item)
                        if isinstance(resp, dict):
                            text = resp.get("text", "")
                            if text:
                                parts.append(text)
                if parts:
                    return [StreamChunk("text", "\n".join(parts))]
            elif isinstance(data, dict):
                resp = data.get("response", data)
                if isinstance(resp, dict):
                    text = resp.get("text", "")
                    if text:
                        return [StreamChunk("text", text)]

            return [StreamChunk("text", raw)]
        except (json.JSONDecodeError, TypeError):
            # Incomplete JSON, keep buffering
            return []


class ClaudeStreamProcessor(BaseStreamProcessor):
    """Claude Code processor — handles thinking blocks and tool calls."""

    agent_name = "claude"

    def __init__(self):
        super().__init__()
        self._in_thinking = False
        self._thinking_lines: list = []

    def process_line(self, line: str) -> list[StreamChunk]:
        stripped = line.strip()

        # ── Thinking block detection ──
        if stripped.startswith("<thinking>") or stripped == "<antThinking>":
            self._in_thinking = True
            self._thinking_lines = []
            return [StreamChunk("thinking", "🧠 Thinking...", {"state": "start"})]

        if stripped.startswith("</thinking>") or stripped == "</antThinking>":
            self._in_thinking = False
            thinking = "\n".join(self._thinking_lines)
            return [StreamChunk("thinking", thinking, {"state": "end"})]

        if self._in_thinking:
            self._thinking_lines.append(line)
            return []  # Buffer thinking, emit when complete

        # ── Tool call patterns (Claude Code format) ──
        if re.match(r'^\s*(?:Read|Write|Edit|Create|Update|Delete|View|List|Search|Bash|Execute)', stripped):
            tool = "file_op"
            if re.match(r'^\s*(?:Bash|Execute|Run)', stripped):
                tool = "shell"
            elif re.match(r'^\s*(?:Search|Grep|Find)', stripped):
                tool = "search"
            return [StreamChunk("tool_use", line, {"tool": tool})]

        # ── Tool result pattern ──
        if re.match(r'^(?:Result|Output|Contents):', stripped):
            return [StreamChunk("tool_result", line)]

        # ── File write ──
        if re.match(r'^(?:Wrote|Created|Updated)\s+', stripped):
            filename = re.search(r'`([^`]+)`', line)
            return [StreamChunk("code_write", line, {"file": filename.group(1) if filename else ""})]

        return super().process_line(line)

    def finalize(self) -> list[StreamChunk]:
        chunks = super().finalize()
        if self._in_thinking and self._thinking_lines:
            chunks.append(StreamChunk("thinking", "\n".join(self._thinking_lines), {"state": "end"}))
            self._in_thinking = False
        return chunks


class CodexStreamProcessor(BaseStreamProcessor):
    """Codex CLI processor — handles reasoning and exec flow."""

    agent_name = "codex"

    def __init__(self):
        super().__init__()
        self._in_reasoning = False

    def process_line(self, line: str) -> list[StreamChunk]:
        stripped = line.strip()

        # ── Skip Codex banner noise ──
        if re.match(r'^(?:OpenAI Codex|workdir:|model:|provider:|approval:|sandbox:|reasoning|session id:)', stripped):
            return [StreamChunk("system", line)]

        # ── Reasoning block ──
        if stripped.startswith("Reasoning:") or stripped.startswith("thinking:"):
            self._in_reasoning = True
            return [StreamChunk("thinking", line.split(":", 1)[1].strip() if ":" in line else line, {"state": "start"})]

        if self._in_reasoning and not stripped:
            self._in_reasoning = False
            return [StreamChunk("thinking", "", {"state": "end"})]

        if self._in_reasoning:
            return [StreamChunk("thinking", line)]

        # ── File ops ──
        if re.match(r'^(?:patch|apply_diff|write_file|read_file)', stripped):
            return [StreamChunk("tool_use", line, {"tool": "file_op"})]

        if re.match(r'^(?:shell|exec|run)', stripped):
            return [StreamChunk("tool_use", line, {"tool": "shell"})]

        return super().process_line(line)


class OllamaStreamProcessor(BaseStreamProcessor):
    """Ollama HTTP processor — minimal wrapping, mostly text."""
    agent_name = "ollama"


class MfluxStreamProcessor(BaseStreamProcessor):
    """MFLUX processor — image generation steps."""
    agent_name = "mflux"

    def process_line(self, line: str) -> list[StreamChunk]:
        stripped = line.strip()
        if re.search(r'(?:step|progress|generating|loading)', stripped, re.IGNORECASE):
            return [StreamChunk("progress", line)]
        return super().process_line(line)


# ─── Processor Registry ────────────────────────────────
PROCESSORS = {
    "gemini": GeminiStreamProcessor,
    "claude": ClaudeStreamProcessor,
    "codex": CodexStreamProcessor,
    "ollama": OllamaStreamProcessor,
    "mflux": MfluxStreamProcessor,
}

def get_processor(agent: str) -> BaseStreamProcessor:
    """Get the appropriate stream processor for an agent."""
    cls = PROCESSORS.get(agent, BaseStreamProcessor)
    return cls()
