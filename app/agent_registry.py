"""
Agent & Skills Discovery Module
Scans local CLI skill directories for Gemini, Claude, and Codex
and returns structured metadata for the frontend.
"""
import os
import re
import subprocess
import json
from typing import List, Dict, Any, Optional
from pathlib import Path

# ─── Skill Directory Locations ──────────────────
SKILL_DIRS = {
    "gemini":  os.path.expanduser("~/.gemini/skills"),
    "claude":  os.path.expanduser("~/.claude/skills"),
    "codex":   os.path.expanduser("~/.codex/skills"),
}

CODEX_CONFIG_PATH = os.path.expanduser("~/.codex/config.toml")
GEMINI_SETTINGS_PATH = os.path.expanduser("~/.gemini/settings.json")
CLAUDE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")


def _parse_skill_md(skill_dir: str) -> Optional[Dict[str, Any]]:
    """Parse a SKILL.md file and extract YAML frontmatter metadata."""
    skill_md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md):
        # Try lowercase
        skill_md = os.path.join(skill_dir, "skill.md")
    if not os.path.isfile(skill_md):
        return None

    try:
        with open(skill_md, "r", errors="replace") as f:
            content = f.read(8192)  # first 8KB is enough
    except Exception:
        return None

    meta = {
        "name": os.path.basename(skill_dir),
        "description": "",
        "path": skill_dir,
    }

    # Parse YAML frontmatter: ---\nkey: value\n---
    fm_match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if fm_match:
        frontmatter = fm_match.group(1)
        for line in frontmatter.split('\n'):
            line = line.strip()
            if line.startswith('name:'):
                val = line[5:].strip().strip('"').strip("'")
                if val:
                    meta["name"] = val
            elif line.startswith('description:'):
                val = line[12:].strip().strip('"').strip("'")
                if val:
                    meta["description"] = val

    # If description is still empty, try to grab first non-empty line after frontmatter
    if not meta["description"]:
        body = content
        if fm_match:
            body = content[fm_match.end():]
        for line in body.split('\n'):
            line = line.strip()
            if line and not line.startswith('#') and not line.startswith('---'):
                meta["description"] = line[:200]
                break

    # Detect file count for skill complexity
    file_count = 0
    for _, _, files in os.walk(skill_dir):
        file_count += len(files)
    meta["file_count"] = file_count

    # Check if symlink
    meta["is_symlink"] = os.path.islink(skill_dir)
    if meta["is_symlink"]:
        try:
            meta["link_target"] = os.readlink(skill_dir)
        except Exception:
            pass

    return meta


def discover_skills(agent: str) -> List[Dict[str, Any]]:
    """Discover installed skills for a specific agent CLI."""
    base = SKILL_DIRS.get(agent)
    if not base or not os.path.isdir(base):
        return []

    skills = []
    for entry in sorted(os.listdir(base)):
        full = os.path.join(base, entry)
        # Skip hidden entries (except .system for codex)
        if entry.startswith('.') and entry != '.system':
            continue
        if not os.path.isdir(full) and not os.path.islink(full):
            continue

        # Resolve symlinks
        resolved = full
        if os.path.islink(full):
            resolved = os.path.realpath(full)
            if not os.path.isdir(resolved):
                continue

        parsed = _parse_skill_md(resolved)
        if parsed:
            parsed["id"] = entry
            parsed["agent"] = agent
            # Use original name if symlink
            if os.path.islink(full):
                parsed["is_symlink"] = True
                parsed["path"] = full
            skills.append(parsed)
        else:
            # Directory exists but no SKILL.md — still record it
            skills.append({
                "id": entry,
                "name": entry,
                "description": "(No SKILL.md found)",
                "agent": agent,
                "path": full,
                "file_count": 0,
                "is_symlink": os.path.islink(full),
            })

    # For codex, also scan .system subdirectory
    if agent == "codex":
        system_dir = os.path.join(base, ".system")
        if os.path.isdir(system_dir):
            for entry in sorted(os.listdir(system_dir)):
                full = os.path.join(system_dir, entry)
                if not os.path.isdir(full):
                    continue
                parsed = _parse_skill_md(full)
                if parsed:
                    parsed["id"] = f".system/{entry}"
                    parsed["agent"] = agent
                    parsed["is_system"] = True
                    skills.append(parsed)

    return skills


def get_agent_config(agent: str) -> Dict[str, Any]:
    """Read agent-specific configuration."""
    config = {}  # type: Dict[str, Any]

    if agent == "gemini":
        if os.path.isfile(GEMINI_SETTINGS_PATH):
            try:
                with open(GEMINI_SETTINGS_PATH) as f:
                    config = json.load(f)
            except Exception:
                pass
        config["model"] = "gemini-2.5-pro"
        config["provider"] = "Google DeepMind"

    elif agent == "claude":
        if os.path.isfile(CLAUDE_SETTINGS_PATH):
            try:
                with open(CLAUDE_SETTINGS_PATH) as f:
                    config = json.load(f)
            except Exception:
                pass
        config["model"] = "claude-sonnet-4"
        config["provider"] = "Anthropic"

    elif agent == "codex":
        if os.path.isfile(CODEX_CONFIG_PATH):
            try:
                # Parse TOML-lite (just main keys)
                with open(CODEX_CONFIG_PATH) as f:
                    for line in f:
                        line = line.strip()
                        if '=' in line and not line.startswith('[') and not line.startswith('#'):
                            k, v = line.split('=', 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k in ('model', 'model_reasoning_effort', 'personality'):
                                config[k] = v
            except Exception:
                pass
        config.setdefault("model", "gpt-5.4")
        config["provider"] = "OpenAI"

    elif agent == "ollama":
        config["provider"] = "Local (Ollama)"
        config["model"] = "multiple"
        # Try to get model list
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        config["base_url"] = ollama_url

    elif agent == "mflux":
        config["provider"] = "Local (MLX)"
        config["model"] = "Qwen-Image-2512-8bit"
        config["base_url"] = os.getenv("MFLUX_BASE_URL", "http://192.168.0.212:8000")

    return config


def get_agent_status(agent: str) -> str:
    """Check if an agent is enabled and reachable."""
    env_map = {
        "gemini": "ENABLE_GEMINI_CLI",
        "claude": "ENABLE_CLAUDE_REMOTE_CONTROL",
        "codex": "ENABLE_CODEX_SERVER",
        "ollama": "ENABLE_OLLAMA_API",
        "mflux": "ENABLE_MFLUX_IMAGE",
    }
    env_key = env_map.get(agent, "")
    if not env_key:
        return "unknown"
    return "active" if os.getenv(env_key) == "true" else "disabled"


def get_all_agents() -> List[Dict[str, Any]]:
    """Return full agent registry with skills and config."""
    agents = []

    definitions = [
        {
            "id": "gemini",
            "name": "Gemini CLI",
            "type": "cli",
            "class": "cloud",
            "color": "#4285f4",
            "icon": "sparkles",
            "description": "Google DeepMind's Gemini model via CLI. Best for code generation, multi-modal reasoning, and agentic tool use with MCP extensions.",
            "capabilities": ["code_generation", "file_operations", "web_search", "mcp_tools", "skills", "multi_modal"],
        },
        {
            "id": "claude",
            "name": "Claude Code",
            "type": "cli",
            "class": "cloud",
            "color": "#d97706",
            "icon": "brain",
            "description": "Anthropic's Claude model via Claude Code CLI. Excellent at careful reasoning, long-context tasks, and structured output.",
            "capabilities": ["code_generation", "file_operations", "skills", "long_context"],
        },
        {
            "id": "codex",
            "name": "Codex CLI",
            "type": "cli",
            "class": "cloud",
            "color": "#10b981",
            "icon": "code",
            "description": "OpenAI's GPT-5.4 via Codex CLI with web search, plugins, and curated skill marketplace.",
            "capabilities": ["code_generation", "file_operations", "web_search", "plugins", "skills"],
        },
        {
            "id": "ollama",
            "name": "Ollama",
            "type": "http",
            "class": "local",
            "color": "#8b5cf6",
            "icon": "server",
            "description": "Local/LAN model inference via Ollama. Run open-weight models (Llama, Qwen, DeepSeek, Gemma) on your own hardware.",
            "capabilities": ["text_generation", "multi_model", "local_inference", "privacy"],
        },
        {
            "id": "mflux",
            "name": "MFLUX Visual",
            "type": "http",
            "class": "local",
            "color": "#ec4899",
            "icon": "image",
            "description": "MLX-accelerated image generation via remote API. Uses Qwen-Image for high-quality visual rendering on Apple Silicon.",
            "capabilities": ["image_generation", "local_inference"],
        },
    ]

    for defn in definitions:
        agent_id = defn["id"]
        entry = {
            **defn,
            "status": get_agent_status(agent_id),
            "config": get_agent_config(agent_id),
            "skills": discover_skills(agent_id),
            "skill_count": 0,
        }
        entry["skill_count"] = len(entry["skills"])
        agents.append(entry)

    return agents
