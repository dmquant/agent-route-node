"""
AGY Temp & Scratch Data Sweeper (`app/agy_sweeper.py`).

Why this exists:
1. Every `agy` hand task posts its output text to ROS (`_post_result`) and
   captures any newly written files from `~/.gemini/antigravity-cli/brain/<conv-id>/`
   and `~/.gemini/antigravity-cli/scratch/` into the session's `workspace_dir`,
   which `workspace_sync.sync_session_now()` immediately uploads to R2 / ROS.
2. Leaving temporary files in `~/.gemini/antigravity-cli/scratch/` and old
   session state in `brain/`, `conversations/`, `annotations/`, `presence/`,
   and `log/` causes unbounded disk bloat (e.g., a single runaway grep redirect
   `found_matches.txt` grew to 3.3 TB in `gubei12.local:~/.gemini/antigravity-cli/scratch/`).
3. This module provides:
   - `sweep_after_agy_task(started_at, conv_id=None)`: immediate post-sync cleanup
     of the just-completed run's scratch files and oversized artifacts.
   - `sweep_agy_temp_now()`: periodic TTL sweep of `scratch/` (default 2h TTL
     or >50MB) and `brain/`, `conversations/`, `annotations/`, `presence/`,
     `log/`, `crashes/` (default 24h TTL), while strictly preserving auth tokens,
     settings, and MCP configs.
   - `start_agy_sweeper()` / `stop_agy_sweeper()`: background asyncio loop
     running every 30 minutes.
"""

import asyncio
import logging
import os
import shutil
import time
from typing import Optional

_logger = logging.getLogger("agent-route.agy-sweeper")

# Configuration knobs (overridable via environment)
AGY_SWEEP_INTERVAL_S = int(os.getenv("AGY_SWEEP_INTERVAL_S", "1800"))        # 30 minutes
AGY_SCRATCH_TTL_S = int(os.getenv("AGY_SCRATCH_TTL_S", "7200"))              # 2 hours
AGY_STATE_TTL_S = int(os.getenv("AGY_STATE_TTL_S", "86400"))                 # 24 hours
MAX_SCRATCH_FILE_BYTES = int(os.getenv("AGY_MAX_SCRATCH_BYTES", str(50 * 1024 * 1024)))  # 50 MB

# Top-level items inside ~/.gemini/antigravity-cli/ that MUST NEVER be deleted
PROTECTED_NAMES = frozenset({
    "antigravity-oauth-token",
    "settings.json",
    "installation_id",
    "mcp",
    "builtin",
    "bin",
    "updater",
    "knowledge",
    "implicit",
    "cli.log",
    "last_check.timestamp",
})

_sweeper_task: Optional[asyncio.Task] = None


def get_agy_data_root() -> str:
    override = os.getenv("AGY_DATA_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.gemini/antigravity-cli")


def sweep_after_agy_task(started_at: float, conv_id: Optional[str] = None) -> dict:
    """Immediately clean up run-scoped scratch files after workspace_sync completes.

    Since `_capture_artifacts()` has already copied the run's deliverables into
    `workspace_dir` and `sync_session_now()` has uploaded them to R2 / ROS,
    keeping the duplicate copies in `~/.gemini/antigravity-cli/scratch/` only
    wastes disk space and risks cross-contaminating future scans.
    """
    root = get_agy_data_root()
    stats = {"scratch_files_removed": 0, "scratch_bytes_freed": 0, "brain_cleaned": False}
    if not os.path.isdir(root):
        return stats

    scratch_dir = os.path.join(root, "scratch")
    if os.path.isdir(scratch_dir):
        for dirpath, dirnames, filenames in os.walk(scratch_dir, topdown=False):
            for fn in filenames:
                fpath = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fpath, follow_symlinks=False)
                    # Remove if created/modified during this run OR if oversized (>50MB)
                    if st.st_mtime >= (started_at - 2.0) or st.st_size > MAX_SCRATCH_FILE_BYTES:
                        os.unlink(fpath)
                        stats["scratch_files_removed"] += 1
                        stats["scratch_bytes_freed"] += st.st_size
                except OSError:
                    pass
            # Prune empty subdirectories inside scratch/ (never scratch/ itself)
            if dirpath != scratch_dir:
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass

    # If conv_id is known, remove heavy .system_generated logs inside brain/<conv_id>/
    if conv_id:
        sys_gen = os.path.join(root, "brain", conv_id, ".system_generated")
        if os.path.isdir(sys_gen):
            try:
                shutil.rmtree(sys_gen, ignore_errors=True)
                stats["brain_cleaned"] = True
            except OSError:
                pass

    if stats["scratch_files_removed"] > 0 or stats["brain_cleaned"]:
        _logger.info(
            "AGY post-task sweep: removed %d scratch file(s) (%.2f MB), brain_cleaned=%s",
            stats["scratch_files_removed"],
            stats["scratch_bytes_freed"] / (1024 * 1024),
            stats["brain_cleaned"],
        )
    return stats


def sweep_agy_temp_now(
    scratch_ttl_s: int = AGY_SCRATCH_TTL_S,
    state_ttl_s: int = AGY_STATE_TTL_S,
) -> dict:
    """Periodic TTL sweep across `~/.gemini/antigravity-cli/`.

    - `scratch/**`: deletes files older than `scratch_ttl_s` (default 2h) or
      larger than `MAX_SCRATCH_FILE_BYTES` (50 MB), plus empty directories.
    - `brain/`, `conversations/`, `annotations/`, `presence/`, `log/`, `crashes/`:
      deletes entries older than `state_ttl_s` (default 24h).
    """
    root = get_agy_data_root()
    now = time.time()
    stats = {
        "scratch_removed": 0,
        "state_removed": 0,
        "bytes_freed": 0,
    }
    if not os.path.isdir(root):
        return stats

    # 1. Sweep scratch/
    scratch_dir = os.path.join(root, "scratch")
    if os.path.isdir(scratch_dir):
        scratch_cutoff = now - scratch_ttl_s
        for dirpath, dirnames, filenames in os.walk(scratch_dir, topdown=False):
            for fn in filenames:
                fpath = os.path.join(dirpath, fn)
                try:
                    st = os.stat(fpath, follow_symlinks=False)
                    if st.st_mtime < scratch_cutoff or st.st_size > MAX_SCRATCH_FILE_BYTES:
                        os.unlink(fpath)
                        stats["scratch_removed"] += 1
                        stats["bytes_freed"] += st.st_size
                except OSError:
                    pass
            if dirpath != scratch_dir:
                try:
                    if not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    pass

    # 2. Sweep transient state directories (brain, conversations, annotations, presence, log, crashes)
    state_cutoff = now - state_ttl_s
    active_log_target = None
    cli_log_link = os.path.join(root, "cli.log")
    if os.path.islink(cli_log_link):
        try:
            active_log_target = os.path.realpath(cli_log_link)
        except OSError:
            active_log_target = None

    for subdir_name in ("brain", "conversations", "annotations", "presence", "log", "crashes"):
        subdir = os.path.join(root, subdir_name)
        if not os.path.isdir(subdir):
            continue
        try:
            entries = os.listdir(subdir)
        except OSError:
            continue
        for entry in entries:
            epath = os.path.join(subdir, entry)
            if active_log_target and os.path.realpath(epath) == active_log_target:
                continue
            try:
                st = os.stat(epath, follow_symlinks=False)
                if st.st_mtime >= state_cutoff:
                    continue
                if os.path.isdir(epath) and not os.path.islink(epath):
                    dir_size = _dir_size_bytes(epath)
                    shutil.rmtree(epath, ignore_errors=True)
                    stats["state_removed"] += 1
                    stats["bytes_freed"] += dir_size
                else:
                    os.unlink(epath)
                    stats["state_removed"] += 1
                    stats["bytes_freed"] += st.st_size
            except OSError:
                pass

    # 3. Sweep stray root-level report/json dump files older than state_ttl_s
    try:
        for entry in os.listdir(root):
            if entry in PROTECTED_NAMES or entry.startswith("conversation_summaries.db") or entry.startswith("jet"):
                continue
            epath = os.path.join(root, entry)
            if os.path.isdir(epath) or os.path.islink(epath):
                continue
            if entry.endswith((".md", ".json", ".txt")):
                try:
                    st = os.stat(epath, follow_symlinks=False)
                    if st.st_mtime < state_cutoff:
                        os.unlink(epath)
                        stats["state_removed"] += 1
                        stats["bytes_freed"] += st.st_size
                except OSError:
                    pass
    except OSError:
        pass

    if stats["scratch_removed"] > 0 or stats["state_removed"] > 0:
        _logger.info(
            "AGY periodic sweep: scratch_removed=%d, state_removed=%d, freed=%.2f MB",
            stats["scratch_removed"],
            stats["state_removed"],
            stats["bytes_freed"] / (1024 * 1024),
        )
        print(
            f"[agy-sweeper] Swept {stats['scratch_removed']} scratch + "
            f"{stats['state_removed']} state entries "
            f"({stats['bytes_freed'] / (1024 * 1024):.1f} MB freed)"
        )
    return stats


def _dir_size_bytes(path: str) -> int:
    total = 0
    for dp, _, fns in os.walk(path):
        for f in fns:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


async def _sweeper_loop() -> None:
    # Initial startup sweep (non-blocking)
    try:
        await asyncio.to_thread(sweep_agy_temp_now)
    except Exception as e:
        _logger.warning("AGY startup sweep error: %s", e)

    while True:
        try:
            await asyncio.sleep(AGY_SWEEP_INTERVAL_S)
            await asyncio.to_thread(sweep_agy_temp_now)
        except asyncio.CancelledError:
            return
        except Exception as e:
            _logger.warning("AGY periodic sweep error: %s", e)


def start_agy_sweeper() -> None:
    global _sweeper_task
    if _sweeper_task is None or _sweeper_task.done():
        _sweeper_task = asyncio.create_task(_sweeper_loop())
        print(
            f"[agy-sweeper] Started (interval={AGY_SWEEP_INTERVAL_S}s, "
            f"scratch_ttl={AGY_SCRATCH_TTL_S}s, state_ttl={AGY_STATE_TTL_S}s)"
        )


def stop_agy_sweeper() -> None:
    global _sweeper_task
    if _sweeper_task:
        _sweeper_task.cancel()
        _sweeper_task = None
