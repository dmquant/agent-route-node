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

from app.hands.base import Hand, HandResult, filter_noise, resolve_cli_path, get_cli_env, utf8_stream_decoder
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


def _parse_conversation_id_from_text(text: str) -> Optional[str]:
    """Extract agy's conversation UUID from already-read --log-file text.

    The brain artifact dir is keyed by this id:
      `server.go:747] Created conversation <uuid>`
    """
    m = re.search(r"Created conversation ([0-9a-fA-F-]{36})", text or "")
    return m.group(1) if m else None


def _parse_conversation_id(log_path: str) -> Optional[str]:
    """Extract agy's conversation UUID from its --log-file path."""
    try:
        with open(log_path, "r", errors="replace") as f:
            return _parse_conversation_id_from_text(f.read())
    except OSError:
        return None


# ── Google geo-block detection ──────────────────────────────────────────
# agy v1.0.16 swallows Google's per-request geo-block into an exit-0 blank
# (empty stdout AND stderr). The rejection text ("FAILED_PRECONDITION (400):
# User location is not supported for the API use.") survives only in the
# --log-file (often absent under agy 1.1.3) and in agy's trajectory DB
# (~/.gemini/antigravity-cli/conversations/<conv-id>.db, table `steps`,
# column `error_details` — a protobuf blob with the message embedded as
# ASCII). We surface it so the node's rate-limit path can route the task to
# cooldown + requeue instead of recording a fake success.
_GEO_BLOCK_MARKERS = (
    "user location is not supported",
    "location is not supported for the api",
    "failed_precondition",
)


def _scan_geo_block(*texts) -> Optional[str]:
    """Return a short one-line excerpt if any text shows a Google geo-block.

    Accepts str or bytes (trajectory-DB error_details is a proto blob with
    the human-readable message embedded as ASCII). Case-insensitive
    substring match against the documented rejection text — robust to proto
    framing and to log-format changes across agy versions.
    """
    for text in texts:
        if not text:
            continue
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8", errors="replace")
        low = text.lower()
        for marker in _GEO_BLOCK_MARKERS:
            idx = low.find(marker)
            if idx == -1:
                continue
            start = text.rfind("\n", 0, idx) + 1
            end = text.find("\n", idx)
            if end == -1:
                end = len(text)
            # Drop non-printable bytes — trajectory-DB error_details is a proto
            # blob, so the marker is wrapped in framing control chars we don't
            # want in the surfaced message.
            raw = "".join(c for c in text[start:end] if c.isprintable() or c == " ")
            excerpt = " ".join(raw.split())
            return excerpt[:300] if excerpt else \
                "User location is not supported for the API use"
    return None


def _trajectory_db_geo_block(conv_id: Optional[str], started_at: float) -> Optional[str]:
    """Best-effort geo-block excerpt from agy's trajectory DB.

    Under agy 1.1.3 the --log-file is often absent, so the conversation id
    can't be parsed from it; agy runs are serialized by _AGY_LOCK, so the
    newest conversation DB touched during this run (mtime >= started_at) is
    this run's. Read-only; any error → None (detection is best-effort and
    must never break execution).
    """
    root = os.path.join(_agy_data_root(), "conversations")
    candidates: list[str] = []
    if conv_id:
        p = os.path.join(root, f"{conv_id}.db")
        if os.path.isfile(p):
            candidates.append(p)
    if not candidates and os.path.isdir(root):
        fresh: list[tuple[float, str]] = []
        try:
            names = os.listdir(root)
        except OSError:
            names = []
        for fn in names:
            if not fn.endswith(".db"):
                continue
            p = os.path.join(root, fn)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt >= started_at - 1.0:
                fresh.append((mt, p))
        fresh.sort(reverse=True)
        candidates = [p for _, p in fresh[:3]]
    for db_path in candidates:
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            try:
                rows = con.execute(
                    "SELECT error_details FROM steps "
                    "WHERE error_details IS NOT NULL AND error_details != ''"
                ).fetchall()
            finally:
                con.close()
        except Exception:
            continue
        for (val,) in rows:
            excerpt = _scan_geo_block(val)
            if excerpt:
                return excerpt
    return None


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
        #  - `--print` (a.k.a. `--prompt`) is a value-taking flag — it consumes
        #    the NEXT token as the prompt. So every other flag MUST come before
        #    it, and the prompt MUST be its immediate value, or agy treats the
        #    next flag string as the prompt and drops the real input.
        args: list[str] = []

        # agy print mode ignores cwd for workspace context — it needs an
        # explicit --add-dir to know where to read/write files. Without it,
        # file ops land in agy's scratch dir, not the session workspace.
        args.extend(["--add-dir", workspace_dir])

        # Auto-approve tool permissions in print mode. REVERSAL of a prior
        # decision: an earlier comment claimed agy "writes files reliably
        # WITHOUT it", which was true before agy's 2026-05-20 release. Since
        # then, print mode SOFT-DENIES the Edit/file tool
        # (tool_confirmation_manager: `Print mode: soft-denying tool
        # confirmation "Edit"` → `approved=false`), so agy declines every file
        # write, produces empty stdout, and exits 0 — the "0% file capture"
        # defect (the twin of the exit-0 silent-blank). Verified on agy 1.1.3:
        # WITHOUT the flag a file-write probe writes nothing + empty stdout;
        # WITH it (`Print mode: --dangerously-skip-permissions set,
        # auto-approving all tool permissions`) the file lands (411B, marker
        # present) and stdout returns the real answer. The flag auto-approves
        # ALL agy tool calls incl. shell — acceptable for this autonomous
        # execution node (same posture as the claude/codex hands), gated by
        # agy's own config allow-list. Disable via AGY_SKIP_PERMISSIONS=false.
        if os.getenv("AGY_SKIP_PERMISSIONS", "true") != "false":
            args.append("--dangerously-skip-permissions")

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
                env=get_cli_env(workspace_dir),
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
            dec = utf8_stream_decoder()
            while True:
                chunk = await process.stderr.read(256)
                if not chunk:
                    tail = dec.decode(b"", final=True)
                    if tail:
                        raw_stderr_chunks.append(tail)
                    break
                text = dec.decode(chunk)
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
            dec = utf8_stream_decoder()
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    tail = dec.decode(b"", final=True)
                    if tail:
                        raw_stdout_chunks.append(tail)
                    break
                raw_stdout_chunks.append(dec.decode(chunk))

        captured_artifacts: list[str] = []
        walkthrough_text: Optional[str] = None
        geo_block_excerpt: Optional[str] = None
        try:
            await asyncio.gather(stream_stderr(), read_stdout())
            exit_code = await process.wait()
        finally:
            # Read agy's internal log ONCE (for conv-id + geo-block scan)
            # before deleting it. Under agy 1.1.3 the --log-file may never be
            # created — that's fine; stderr and the trajectory DB are the
            # fallback signals.
            try:
                with open(log_path, "r", errors="replace") as _lf:
                    log_text = _lf.read()
            except OSError:
                log_text = ""
            conv_id = _parse_conversation_id_from_text(log_text)

            # Capture artifacts BEFORE deleting the log (we need the conv-id)
            # and BEFORE releasing the lock (so the scratch scan isn't racing
            # the next serialized agy run).
            if os.getenv("AGY_CAPTURE_ARTIFACTS", "true") != "false":
                try:
                    captured_artifacts, walkthrough_text = _capture_artifacts(
                        workspace_dir, conv_id, started_at
                    )
                except Exception:
                    pass

            # Detect a swallowed Google geo-block (the root-cause failure agy
            # turns into an exit-0 blank). Signal priority: the log, the
            # streamed stderr, then agy's trajectory DB error_details. MUST
            # run before _safe_unlink while the log still exists.
            geo_block_excerpt = _scan_geo_block(log_text, ''.join(raw_stderr_chunks))
            if not geo_block_excerpt:
                geo_block_excerpt = _trajectory_db_geo_block(conv_id, started_at)

            _safe_unlink(log_path)
            _AGY_LOCK.release()

        raw_stdout = ''.join(raw_stdout_chunks)
        raw_stderr = ''.join(raw_stderr_chunks)

        output_text = filter_noise(raw_stdout).strip()
        # filter_noise can theoretically swallow a whole valid markdown
        # answer (agy emits plain markdown; cross-hand noise patterns like
        # `---` rules or `model:`-shaped lines can match). If filtering
        # emptied a non-empty stdout, restore the raw stdout rather than lose
        # the answer (mirrors gemini_hand.py's exit-0 raw-stdout restore).
        if not output_text and raw_stdout.strip():
            output_text = raw_stdout.strip()

        # Did agy produce a genuine answer? Captured BEFORE we substitute
        # error text below, so the empty-result guard can't be fooled by a
        # surfaced stderr / geo-block excerpt.
        produced_answer = bool(output_text) or bool(walkthrough_text)

        # Empty output is NOT success — regardless of exit code. Substitute,
        # in priority order: streamed stderr, then a geo-block excerpt
        # recovered from the log / trajectory DB. Previously stderr was only
        # substituted when exit_code != 0, so agy's exit-0 empty blanks (the
        # swallowed geo-block) fell straight through to the "Exit code 0"
        # literal the worker trusted as a completed success — the incident.
        if not output_text:
            if raw_stderr.strip():
                output_text = raw_stderr.strip()
            elif geo_block_excerpt:
                # agy swallowed Google's per-request geo-block: exit 0, empty
                # stdout AND stderr; the only trace is the log / trajectory
                # DB. Surface it as plain error text — rate_limit._parse_agy
                # classifies it and task_puller structures it into the
                # canonical [RATE_LIMITED hand=agy retry_after_s=300
                # reason=geo_block] sentinel the worker routes to cooldown +
                # requeue. We deliberately do NOT hardcode that sentinel here,
                # to avoid double-prefixing the node's existing rate-limit
                # path (task_puller.py prepends it on a parse_rate_limit hit).
                output_text = f"agy backend error: {geo_block_excerpt}"

        # Inline the agy walkthrough (a concise findings/verification summary)
        # so downstream clients see it without fetching the synced file.
        if walkthrough_text:
            output_text = (
                f"{output_text}\n\n---\n\n## Agy Walkthrough\n\n{walkthrough_text.strip()}"
            ).strip()

        # Interface contract "empty ≠ success": if agy produced no real
        # answer, this must never read as a success. Force a non-zero exit
        # and, if we still have nothing to surface, a descriptive failure —
        # never the bare "Exit code {code}" fallback that started this
        # incident. Belt-and-braces with agent-route's Phase 0 sentinel
        # guard: the node stops PRODUCING the silent blank, the worker stops
        # TRUSTING it.
        if not produced_answer:
            if exit_code == 0:
                exit_code = 1
            if not output_text:
                output_text = (
                    f"[agy] empty result — no output on stdout or stderr and "
                    f"no error recovered from the log or trajectory DB (agy "
                    f"exit {exit_code}); treated as failure, not success."
                )

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
                    if fn == "walkthrough.md":
                        walkthrough_text = content
                    os.makedirs(dest, exist_ok=True)
                    dest_path = os.path.join(dest, fn)
                    # Only (re)write when content changed, so an unchanged
                    # artifact keeps its mtime and workspace_sync skips
                    # re-uploading it. agy regenerates these every run; without
                    # this guard each run re-PUTs identical files → R2 Class-A
                    # churn (mirrors the institute's archiveSessionWorkspace
                    # skip-if-unchanged fix).
                    existing: Optional[str] = None
                    if os.path.exists(dest_path):
                        try:
                            with open(dest_path, "r", errors="replace") as f:
                                existing = f.read()
                        except OSError:
                            existing = None
                    if existing != content:
                        with open(dest_path, "w") as f:
                            f.write(content)
                    captured.append(f"agy_artifacts/{fn}")
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
