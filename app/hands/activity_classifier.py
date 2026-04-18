"""Activity Classifier — detect and annotate agent activities from raw output.

Parses raw CLI stderr/stdout lines from agent processes to detect high-level
activities: tool calls, file operations, web searches, shell commands,
skill usage, function calls, and other structured actions.

These are emitted as structured chunks to the frontend for rich rendering.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Activity:
    """A classified agent activity."""
    type: str           # tool_call, file_op, shell_cmd, web_search, skill_use, thinking, read_file, write_file
    label: str          # Human readable short label
    detail: str         # Full detail text
    icon: str           # Suggested icon name for the frontend
    metadata: dict      # Structured data (file path, command, url, etc.)

    def to_chunk(self) -> dict:
        """Convert to WebSocket chunk payload."""
        return {
            "chunkType": "activity",
            "content": self.detail,
            "meta": {
                "activityType": self.type,
                "label": self.label,
                "icon": self.icon,
                **self.metadata,
            }
        }


# ─── Activity Detection Patterns ──────────────────────────────

# Gemini CLI activity patterns (stderr)
_GEMINI_PATTERNS = [
    # Tool/function calls (Gemini format)
    (re.compile(r'(?:Using tool|Calling|Invoking|Tool call)[\s:]+[`"\']?(\w+)[`"\']?', re.IGNORECASE),
     lambda m: Activity("tool_call", m.group(1), m.group(0), "wrench",
                        {"tool": m.group(1)})),

    # Read file operations
    (re.compile(r'(?:Reading|Read|Viewing)\s+(?:file\s+)?[`"\']?([^\s`"\']+\.\w+)[`"\']?', re.IGNORECASE),
     lambda m: Activity("read_file", f"Read {_basename(m.group(1))}", m.group(0), "file-text",
                        {"file": m.group(1), "op": "read"})),

    # Write/create/edit file operations
    (re.compile(r'(?:Writing|Wrote|Creating|Created|Editing|Edited|Modified|Updating|Updated)\s+(?:file\s+)?[`"\']?([^\s`"\']+\.\w+)[`"\']?', re.IGNORECASE),
     lambda m: Activity("write_file", f"Write {_basename(m.group(1))}", m.group(0), "file-edit",
                        {"file": m.group(1), "op": "write"})),

    # Shell command execution
    (re.compile(r'(?:Running|Executing|Ran|Exec)\s+(?:command|shell|bash|in shell)[\s:]+[`"\']?(.+?)[`"\']?\s*$', re.IGNORECASE),
     lambda m: Activity("shell_cmd", "Shell Command", m.group(0), "terminal",
                        {"command": m.group(1).strip()})),

    # Shell - just backtick commands
    (re.compile(r'^(?:>|\$)\s+(.+)$'),
     lambda m: Activity("shell_cmd", "Shell", m.group(0), "terminal",
                        {"command": m.group(1).strip()})),

    # Web/URL fetch
    (re.compile(r'(?:Fetching|Searching|Browsing|Opening|GET|POST)\s+(?:URL\s+)?(?:https?://[^\s]+)', re.IGNORECASE),
     lambda m: Activity("web_search", "Web Request", m.group(0), "globe",
                        {"url": _extract_url(m.group(0))})),

    # Google search / web search
    (re.compile(r'(?:Searching\s+(?:the\s+)?web|Google\s+search|Web\s+search)\s+(?:for\s+)?[`"\']?(.+?)[`"\']?\s*$', re.IGNORECASE),
     lambda m: Activity("web_search", "Web Search", m.group(0), "search",
                        {"query": m.group(1).strip()})),

    # Grep / search in files
    (re.compile(r'(?:Searching|Grep(?:ping)?|Find(?:ing)?)\s+(?:for\s+|in\s+)?[`"\']?(.+?)(?:[`"\']?\s+in\s+[`"\']?([^\s`"\']+)[`"\']?)?$', re.IGNORECASE),
     lambda m: Activity("search_code", "Search", m.group(0), "search",
                        {"query": m.group(1).strip(), "path": m.group(2) or ""})),

    # Skill/extension use
    (re.compile(r'(?:Using skill|Loading skill|Skill)\s*[:\s]+[`"\']?(\w[\w\-_]*)[`"\']?', re.IGNORECASE),
     lambda m: Activity("skill_use", f"Skill: {m.group(1)}", m.group(0), "sparkles",
                        {"skill": m.group(1)})),

    # MCP tool calls
    (re.compile(r'(?:MCP|mcp)\s*(?:tool\s+)?(?:call|invoke|exec)\s*[:\s]+[`"\']?(\w+)[`"\']?', re.IGNORECASE),
     lambda m: Activity("tool_call", f"MCP: {m.group(1)}", m.group(0), "plug",
                        {"tool": m.group(1), "transport": "mcp"})),

    # Thinking / planning
    (re.compile(r'^(?:Thinking|Planning|Analyzing|Considering|Reasoning)', re.IGNORECASE),
     lambda m: Activity("thinking", "Thinking", m.group(0), "brain",
                        {})),

    # Diff / patch apply
    (re.compile(r'(?:Applying|Applied)\s+(?:diff|patch|changes)\s+(?:to\s+)?[`"\']?([^\s`"\']*)[`"\']?', re.IGNORECASE),
     lambda m: Activity("write_file", f"Patch {_basename(m.group(1))}", m.group(0), "git-merge",
                        {"file": m.group(1), "op": "patch"})),

    # Directory listing
    (re.compile(r'(?:Listing|List)\s+(?:directory|dir|contents)\s+[`"\']?([^\s`"\']+)[`"\']?', re.IGNORECASE),
     lambda m: Activity("read_file", f"List {_basename(m.group(1))}", m.group(0), "folder-open",
                        {"file": m.group(1), "op": "list"})),
]

# Claude Code activity patterns (stdout — Claude is more verbose)
_CLAUDE_PATTERNS = [
    (re.compile(r'^Read\s+(.+\.\w+)'),
     lambda m: Activity("read_file", f"Read {_basename(m.group(1))}", m.group(0), "file-text",
                        {"file": m.group(1), "op": "read"})),

    (re.compile(r'^(?:Write|Edit|Create|Update)\s+(.+\.\w+)'),
     lambda m: Activity("write_file", f"Write {_basename(m.group(1))}", m.group(0), "file-edit",
                        {"file": m.group(1), "op": "write"})),

    (re.compile(r'^Bash\s+(.+)$'),
     lambda m: Activity("shell_cmd", "Shell Command", m.group(0), "terminal",
                        {"command": m.group(1).strip()})),

    (re.compile(r'^Search\s+(.+)$'),
     lambda m: Activity("search_code", "Search", m.group(0), "search",
                        {"query": m.group(1).strip()})),
]

# Codex activity patterns
_CODEX_PATTERNS = [
    (re.compile(r'^(?:shell|exec)\s+(.+)$'),
     lambda m: Activity("shell_cmd", "Shell Command", m.group(0), "terminal",
                        {"command": m.group(1).strip()})),

    (re.compile(r'^(?:read_file|patch|apply_diff|write_file)\s+(.+)$'),
     lambda m: Activity("write_file", f"File: {_basename(m.group(1))}", m.group(0), "file-edit",
                        {"file": m.group(1), "op": "write"})),
]


# ─── Agent → Pattern mapping ──────────────────────

AGENT_PATTERNS = {
    "gemini": _GEMINI_PATTERNS,
    "claude": _CLAUDE_PATTERNS,
    "codex": _CODEX_PATTERNS,
}


def classify_line(line: str, agent: str = "gemini") -> Optional[Activity]:
    """Try to classify a raw output line as a structured activity.
    
    Returns an Activity if the line matches a known pattern, None otherwise.
    """
    stripped = line.strip()
    if not stripped:
        return None

    patterns = AGENT_PATTERNS.get(agent, _GEMINI_PATTERNS)
    for pattern, factory in patterns:
        match = pattern.search(stripped)
        if match:
            try:
                return factory(match)
            except Exception:
                continue

    # Fallback: try common patterns that apply to all agents
    for pattern, factory in _GEMINI_PATTERNS:
        match = pattern.search(stripped)
        if match:
            try:
                return factory(match)
            except Exception:
                continue

    return None


# ─── Helpers ──────────────────────

def _basename(path: str) -> str:
    """Extract filename from path."""
    return path.rstrip('/').split('/')[-1].split('\\')[-1] if path else ""

def _extract_url(text: str) -> str:
    """Extract URL from text."""
    match = re.search(r'https?://[^\s\'"]+', text)
    return match.group(0) if match else ""
