"""Antigravity (agy) CLI Hand — Google's Gemini agent via the new `agy` CLI.

The `agy` binary is Google Antigravity's CLI, slated to replace the legacy
`gemini` CLI on 2026-06-18. It is gemini-powered but ships as a native Go
binary (not npx), authenticates via OS keyring, and emits plain markdown
on stdout (no --output-format json).

This hand runs `agy --add-dir <ws> --print <prompt>` per task (stdin sent to
/dev/null so agy can't block on an interactive read), and streams stderr in
real-time for progress. Model selection happens inside agy's stored config —
there is no per-invocation --model flag.

Two agy quirks need special handling so output rides the CF workspace sync:

  1. Print mode ignores cwd for the file workspace — we pass `--add-dir
     <workspace_dir>` so the main deliverable lands in the synced dir.
  2. Agentic runs write extra artifacts (task.md, implementation_plan.md,
     walkthrough.md) to ~/.gemini/antigravity-cli/brain/<conversation-id>/,
     OUTSIDE the workspace. We parse the conversation-id from the --log-file
     and copy those into workspace_dir/agy_artifacts/ so they sync, and we
     inline the walkthrough into the result text. As a safety net we also
     scavenge any run-fresh files agy may have dropped in its scratch dir.
"""

import asyncio
import os
import re
import json
import tempfile
import time
from typing import Optional, Callable, Any

from app.hands.base import Hand, HandResult, filter_noise, resolve_cli_path, get_cli_env
from app.hands.stream_processor import AgyStreamProcessor


# agy does NOT tolerate concurrent invocations: instances share one
# ~/.gemini/antigravity-cli data dir, a language-server port, and the OS
# keyring. Running two at once makes one stall with an empty log (the
# "auth timeout" the institute saw) and/or makes agy abandon --add-dir and
# dump output into its scratch dir. We serialize all agy runs on a node with
# this process-wide lock (the edge node is a single uvicorn process, so an
# asyncio.Lock is sufficient). Throughput cost is acceptable — correctness
# over speed; scale out with more nodes if needed.
_AGY_LOCK = asyncio.Lock()


def _agy_data_root() -> str:
    """Antigravity CLI data dir — holds scratch/ and brain/<conv-id>/."""
    return os.path.expanduser("~/.gemini/antigravity-cli")


def _parse_conversation_id(log_path: str) -> Optional[str]:
    """Extract agy's conversation UUID from its --log-file.

    The brain artifact dir is keyed by this id:
      `server.go:747] Created conversation <uuid>`
    """
    try:
        with open(log_path, "r", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"Created conversation ([0-9a-fA-F-]{36})", text)
    return m.group(1) if m else None


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
        os.makedirs(workspace_dir, exist_ok=True)
        await self._ensure_git(workspace_dir)

        cmd = resolve_cli_path("agy")
        # NOTE on flags:
        #  - We do NOT pass --dangerously-skip-permissions. It doesn't actually
        #    bypass agy's core tool review in print mode (agy says so itself),
        #    and it nudges agy toward interactive confirmation code paths. The
        #    behaviour matrix showed agy writes files reliably WITHOUT it.
        #  - `--print` (a.k.a. `--prompt`) is a value-taking flag — it consumes
        #    the NEXT token as the prompt. So every other flag MUST come before
        #    it, and the prompt MUST be its immediate value, or agy treats the
        #    next flag string as the prompt and drops the real input.
        args: list[str] = []

        # agy print mode ignores cwd for workspace context — it needs an
        # explicit --add-dir to know where to read/write files. Without it,
        # file ops land in agy's scratch dir, not the session workspace.
        args.extend(["--add-dir", workspace_dir])

        # agy does NOT accept --model in --print mode (the flag is unknown
        # and the binary exits non-zero). Model is set via agy's UI config.
        # If a caller passes `model=`, we record it for traceability but
        # don't forward it to the subprocess.

        # Per-request timeout. agy's built-in --print-timeout default is only
        # 5m, which cuts off heavy bilingual research BEFORE it writes its
        # files — the model streams a partial summary and exits, so no report
        # lands on disk (the failure the institute saw). Default to 1800s
        # (30m). Override via AGY_PRINT_TIMEOUT (accepts Go duration, e.g.
        # "1800s" or "30m"). NOTE: keep this <= the worker's 30-min zombie
        # reaper so a legit long run finishes + syncs before being reaped.
        timeout_s = os.getenv("AGY_PRINT_TIMEOUT", "1800s")
        args.extend(["--print-timeout", timeout_s])

        # Capture agy's internal logs to a tmpfile so they don't pollute
        # the workspace and so we can clean up after.
        log_fd, log_path = tempfile.mkstemp(prefix="agy-", suffix=".log")
        os.close(log_fd)
        args.extend(["--log-file", log_path])

        # MUST be last: `--print <prompt>` — the prompt is this flag's value.
        args.extend(["--print", input])

        if on_log:
            short_dir = os.path.basename(workspace_dir)[:12]
            note = f"⚡ Executing with **agy** (workspace: `{short_dir}…`)"
            if model:
                note += f" — requested model `{model}` (note: agy ignores per-call model)"
            await on_log(json.dumps({"chunkType": "progress", "content": note}))

        # Serialize: only one agy subprocess runs per node at a time. Held
        # through artifact capture so the scratch scan sees only this run's
        # files. Acquired here (not earlier) so the cheap setup above isn't
        # serialized. started_at is taken after acquire so the scratch
        # mtime filter excludes any prior serialized run's files.
        if on_log and _AGY_LOCK.locked():
            await on_log(json.dumps({
                "chunkType": "system",
                "content": "⏳ agy busy on this node — queued (agy runs serially)",
            }))
        await _AGY_LOCK.acquire()
        started_at = time.time()

        try:
            process = await asyncio.create_subprocess_exec(
                cmd, *args,
                # CRITICAL: redirect stdin to /dev/null. Without this the child
                # inherits the node process's stdin; agy's print mode does a
                # blocking read() on stdin at startup and, with an open/empty
                # stdin, hangs forever — a 0-byte log, ~0 CPU stall (the
                # "auth timeout" the institute saw). DEVNULL makes that read
                # return EOF immediately, so agy proceeds non-interactively.
                stdin=asyncio.subprocess.DEVNULL,
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
            _AGY_LOCK.release()
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

        captured_artifacts: list[str] = []
        walkthrough_text: Optional[str] = None
        try:
            await asyncio.gather(stream_stderr(), read_stdout())
            exit_code = await process.wait()
        finally:
            # Capture artifacts BEFORE deleting the log (we need the conv-id)
            # and BEFORE releasing the lock (so the scratch scan isn't racing
            # the next serialized agy run).
            if os.getenv("AGY_CAPTURE_ARTIFACTS", "true") != "false":
                try:
                    conv_id = _parse_conversation_id(log_path)
                    captured_artifacts, walkthrough_text = _capture_artifacts(
                        workspace_dir, conv_id, started_at
                    )
                except Exception:
                    pass
            _safe_unlink(log_path)
            _AGY_LOCK.release()

        raw_stdout = ''.join(raw_stdout_chunks)
        output_text = filter_noise(raw_stdout).strip()

        if not output_text and exit_code != 0:
            stderr_text = ''.join(raw_stderr_chunks).strip()
            if stderr_text:
                output_text = stderr_text

        # Inline the agy walkthrough (a concise findings/verification summary)
        # so downstream clients see it without fetching the synced file.
        if walkthrough_text:
            output_text = (
                f"{output_text}\n\n---\n\n## Agy Walkthrough\n\n{walkthrough_text.strip()}"
            ).strip()

        # Emit the final output as a single text chunk so the frontend's
        # OutputParser can render markdown / code blocks.
        if output_text and on_log:
            await on_log(json.dumps({"chunkType": "text", "content": output_text}))

        if captured_artifacts and on_log:
            await on_log(json.dumps({
                "chunkType": "system",
                "content": f"📎 Captured {len(captured_artifacts)} agy artifact(s): "
                           + ", ".join(captured_artifacts),
            }))

        return HandResult(
            output=output_text or f"Exit code {exit_code}",
            exit_code=exit_code,
            artifacts=captured_artifacts,
        )

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


def _capture_artifacts(
    workspace_dir: str, conv_id: Optional[str], started_at: float
) -> tuple[list[str], Optional[str]]:
    """Pull agy's out-of-workspace outputs into workspace_dir so they sync.

    Returns (captured_relpaths, walkthrough_text).

    Two sources, both written by agy OUTSIDE the --add-dir workspace:
      • brain artifacts  → ~/.gemini/antigravity-cli/brain/<conv-id>/*.md
                           copied into workspace_dir/agy_artifacts/
      • scratch fallback → ~/.gemini/antigravity-cli/scratch/** files whose
                           mtime is newer than this run's start. agy often
                           ignores --add-dir under load and writes the real
                           deliverable here, so we MIRROR these to the
                           workspace ROOT (preserving subpaths) — NOT into a
                           prefixed subdir. The institute reads reports at
                           whiteboard/<board>/card-NN/report.zh.md; parking
                           them under agy_scratch/ made them invisible (the
                           bug behind the 404s). Board-ids are unique per
                           session, so root-mirroring won't collide across
                           sessions, and the skip-if-exists guard protects
                           anything agy already wrote to the workspace.

    NOTE: scratch scan is mtime-scoped to this run. Concurrent agy runs are
    serialized by _AGY_LOCK in execute(), so scratch holds only this run's
    files during the scan.
    """
    captured: list[str] = []
    walkthrough_text: Optional[str] = None
    data_root = _agy_data_root()

    # 1. Brain artifacts (task.md, implementation_plan.md, walkthrough.md, …)
    if conv_id:
        brain_dir = os.path.join(data_root, "brain", conv_id)
        if os.path.isdir(brain_dir):
            dest = os.path.join(workspace_dir, "agy_artifacts")
            for fn in sorted(os.listdir(brain_dir)):
                if not fn.endswith(".md") or fn.endswith(".metadata.json"):
                    continue
                src = os.path.join(brain_dir, fn)
                if not os.path.isfile(src):
                    continue
                try:
                    with open(src, "r", errors="replace") as f:
                        content = f.read()
                    os.makedirs(dest, exist_ok=True)
                    with open(os.path.join(dest, fn), "w") as f:
                        f.write(content)
                    captured.append(f"agy_artifacts/{fn}")
                    if fn == "walkthrough.md":
                        walkthrough_text = content
                except OSError:
                    pass

    # 2. Scratch safety net — copy run-fresh files into the workspace.
    scratch_dir = os.path.join(data_root, "scratch")
    if os.path.isdir(scratch_dir):
        for root, dirs, files in os.walk(scratch_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in files:
                if fn.startswith("."):
                    continue
                src = os.path.join(root, fn)
                try:
                    if os.path.getmtime(src) < started_at:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(src, scratch_dir)
                dst = os.path.join(workspace_dir, rel)
                # Don't clobber a file agy already wrote to the workspace
                # directly (via --add-dir) — that copy is authoritative.
                if os.path.exists(dst):
                    continue
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                        fdst.write(fsrc.read())
                    captured.append(rel)
                except OSError:
                    pass

    return captured, walkthrough_text
