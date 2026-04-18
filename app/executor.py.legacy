import asyncio
import os
import sys
import json
import re
import httpx
from typing import Callable, Any

# ─── Noise Patterns to Filter ──────────────────────────
# Known CLI boilerplate lines that pollute user-facing output
_NOISE_PATTERNS = [
    # Gemini SDK internals
    re.compile(r'Timeout of \d+ exceeds the interval'),
    re.compile(r"The 'metricReader' option is deprecated"),
    re.compile(r'Loaded cached credentials'),
    re.compile(r'Loading extension:'),
    re.compile(r'Scheduling MCP context refresh'),
    re.compile(r'Executing MCP context refresh'),
    re.compile(r'MCP context refresh complete'),
    re.compile(r'Error executing tool \w+: Tool .* not found'),
    re.compile(r'\[LocalAgentExecutor\] Skipping subagent tool'),
    re.compile(r'\[LocalAgentExecutor\] Blocked call'),
    # Codex startup banner
    re.compile(r'Reading additional input from stdin'),
    re.compile(r'^-+$'),  # separator lines "--------"
    re.compile(r'^OpenAI Codex v[\d.]+'),
    re.compile(r'^workdir:'),
    re.compile(r'^model:'),
    re.compile(r'^provider:'),
    re.compile(r'^approval:'),
    re.compile(r'^sandbox:'),
    re.compile(r'^reasoning effort:'),
    re.compile(r'^reasoning summaries:'),
    re.compile(r'^session id:'),
    re.compile(r'codex_core_skills::loader: failed to stat skills entry'),
    # Generic npx noise
    re.compile(r'^npm warn'),
    re.compile(r'^npm notice'),
]


def _filter_noise(text: str) -> str:
    """Remove known CLI boilerplate noise lines while preserving meaningful output."""
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned.append(line)
            continue
        if any(p.search(stripped) for p in _NOISE_PATTERNS):
            continue
        cleaned.append(line)
    result = '\n'.join(cleaned)
    # Collapse 3+ consecutive blank lines down to 2
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result


def _parse_gemini_json_output(raw: str) -> str:
    """Extract the response text from Gemini's --output-format json output.
    
    Gemini outputs JSON like: [{"response": {"text": "..."}}]
    or line-delimited JSON objects. We extract the text content.
    """
    # Try parsing as JSON array first
    try:
        data = json.loads(raw.strip())
        if isinstance(data, list):
            parts = []
            for item in data:
                if isinstance(item, dict):
                    resp = item.get('response', item)
                    if isinstance(resp, dict):
                        parts.append(resp.get('text', ''))
                    elif isinstance(resp, str):
                        parts.append(resp)
            if parts:
                return '\n'.join(parts)
        elif isinstance(data, dict):
            resp = data.get('response', data)
            if isinstance(resp, dict):
                return resp.get('text', raw)
    except (json.JSONDecodeError, TypeError):
        pass
    
    # Fallback: try extracting from stream-json (line-delimited)
    parts = []
    for line in raw.strip().split('\n'):
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
            if isinstance(chunk, dict):
                text = chunk.get('text', '') or chunk.get('response', {}).get('text', '')
                if text:
                    parts.append(text)
        except (json.JSONDecodeError, TypeError):
            parts.append(line)
    
    return '\n'.join(parts) if parts else raw


async def run_cli_client(
    client_name: str,
    prompt: str,
    workspace_dir: str,
    on_log: Callable[[str], Any],
    **kwargs
) -> dict:
    """Executes the CLI specific to the client and routes stdout securely to on_log."""
    cmd = ""
    args = []

    if client_name == "gemini":
        cmd = "npx"
        skills_dir = os.path.expanduser("~/.gemini/skills")
        args = [
            "gemini", "-p", prompt,
            "--output-format", "json",
            "--yolo",  # Auto-approve all tool calls in headless mode
            "--include-directories", skills_dir,
        ]
    elif client_name == "claude":
        cmd = "npx"
        args = ["@anthropic-ai/claude-code", "-p", "--dangerously-skip-permissions", prompt]
    elif client_name == "codex":
        cmd = "npx"
        args = ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", prompt]
    else:
        cmd = "echo"
        args = [f"Unknown client requested: {client_name}. Falling back to default mock logging."]

    # Enforce workspace directory creation
    os.makedirs(workspace_dir, exist_ok=True)

    # Initialize git repo if missing — CLI agents need git context
    git_dir = os.path.join(workspace_dir, '.git')
    if not os.path.exists(git_dir):
        try:
            init_proc = await asyncio.create_subprocess_exec(
                'git', 'init',
                cwd=workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await init_proc.wait()
        except Exception:
            pass  # Non-fatal

    # Send a compact system log (hide full path in metadata, show concise info)
    short_dir = os.path.basename(workspace_dir)[:12]
    await on_log(f"⚡ Executing with **{client_name}** (workspace: `{short_dir}…`)\n")

    full_output = []

    # -----------------------
    # NATIVE HTTP STREAMING (Ollama)
    # -----------------------
    if client_name == "ollama":
        target_model = kwargs.get("model", "llama3")
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        await on_log(f"Connecting to `{ollama_url}` → model: **{target_model}**\n\n")
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "POST", 
                    f"{ollama_url}/api/generate", 
                    json={"model": target_model, "prompt": prompt},
                    timeout=180.0
                ) as response:
                    if response.status_code != 200:
                        error_msg = f"Ollama API Error: HTTP {response.status_code}"
                        await on_log(f"\n❌ {error_msg}\n")
                        return {"output": error_msg, "exitCode": 1}
                    
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                chunk = json.loads(line)
                                text = chunk.get("response", "")
                                full_output.append(text)
                                await on_log(text)
                            except json.JSONDecodeError:
                                pass
            return {"output": "".join(full_output), "exitCode": 0}
        except Exception as e:
            error_msg = f"Failed to connect to Ollama: {e}"
            await on_log(f"\n❌ {error_msg}\n")
            return {"output": error_msg, "exitCode": 1}

    # -----------------------
    # NATIVE HTTP STREAMING (MFLUX Remote Graphic Rendering)
    # -----------------------
    if client_name == "mflux":
        mflux_url = os.getenv("MFLUX_BASE_URL", "http://192.168.0.212:8000")
        await on_log(f"Connecting to MFLUX at `{mflux_url}`…\n⏳ This may take up to 45s depending on cache state.\n\n")
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{mflux_url}/generate",
                    json={
                        "prompt": prompt,
                        "width": 1024, "height": 1024,
                        "steps": 30, "guidance": 4.5,
                        "num_images": 1, "format": "png"
                    },
                    timeout=None
                )
                if resp.status_code != 200:
                    error_msg = f"MFLUX API Error: HTTP {resp.status_code}"
                    await on_log(f"\n❌ {error_msg}\n")
                    return {"output": error_msg, "exitCode": 1}
                
                payload = resp.json()
                images = payload.get("images", [])
                if not images:
                    error_msg = "MFLUX returned 200 but no image data."
                    await on_log(f"\n❌ {error_msg}\n")
                    return {"output": error_msg, "exitCode": 1}
                
                b64 = images[0].get("b64", "")
                await on_log("✅ Image generated successfully.\n")
                return {"output": "Image generated.", "exitCode": 0, "image_b64": b64}
        except Exception as e:
            error_name = type(e).__name__
            error_msg = f"MFLUX connection failed ({error_name}): {e}"
            await on_log(f"\n❌ {error_msg}\n")
            return {"output": error_msg, "exitCode": 1}

    # -----------------------
    # OS NATIVE CLI DRIVERS
    # -----------------------
    try:
        process = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace_dir
        )
    except Exception as e:
        error_msg = f"Failed to spawn `{client_name}`: {e}"
        await on_log(f"❌ {error_msg}\n")
        return {"output": error_msg, "exitCode": 1}

    raw_stdout = []
    raw_stderr = []

    async def read_stdout(stream):
        while True:
            line = await stream.read(4096)
            if not line:
                break
            text = line.decode('utf-8', errors='replace')
            raw_stdout.append(text)

    async def read_stderr(stream):
        while True:
            line = await stream.read(4096)
            if not line:
                break
            text = line.decode('utf-8', errors='replace')
            raw_stderr.append(text)

    # Read stdout and stderr concurrently
    await asyncio.gather(
        read_stdout(process.stdout),
        read_stderr(process.stderr)
    )
    
    exit_code = await process.wait()

    # ─── Post-process output per agent ─────
    stdout_text = "".join(raw_stdout)
    stderr_text = "".join(raw_stderr)

    if client_name == "gemini":
        # Parse JSON output → extract response text
        parsed = _parse_gemini_json_output(stdout_text)
        # Filter any remaining noise from stderr that leaked through
        output_text = _filter_noise(parsed)
    elif client_name == "codex":
        # Codex outputs to stdout with a banner preamble — filter it
        combined = stdout_text + stderr_text
        output_text = _filter_noise(combined)
        # Strip Codex "user\n<prompt>" echo block
        output_text = re.sub(r'^user\n.*?\nassistant\n', '', output_text, flags=re.DOTALL)
    elif client_name == "claude":
        # Claude outputs clean text to stdout, errors to stderr
        if exit_code != 0 and not stdout_text.strip():
            # Auth errors or failures — parse the error
            error_clean = _filter_noise(stderr_text)
            # Try to extract JSON error message
            try:
                err_match = re.search(r'\{.*"message"\s*:\s*"([^"]+)"', stderr_text)
                if err_match:
                    output_text = f"❌ Claude Error: {err_match.group(1)}"
                else:
                    output_text = error_clean or stderr_text
            except Exception:
                output_text = stderr_text
        else:
            output_text = stdout_text
    else:
        output_text = _filter_noise(stdout_text + stderr_text)

    # Clean up leading/trailing whitespace
    output_text = output_text.strip()

    if output_text:
        await on_log(output_text)
        full_output.append(output_text)
    elif exit_code != 0:
        fallback = f"Process exited with code {exit_code} (no output captured)."
        await on_log(fallback)
        full_output.append(fallback)

    return {"output": "".join(full_output), "exitCode": exit_code}
