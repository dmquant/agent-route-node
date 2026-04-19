"""
Workspace Sync — watches local workspace directories and uploads
new/modified files to the CF Worker's R2 storage.

Uses file mtime for incremental sync. State persisted to disk
so restarts don't trigger full re-uploads.
"""
import os
import json
import asyncio
from pathlib import Path
import httpx

from app.config import get_workspaces_dir, get_data_dir
WORKSPACES_DIR = get_workspaces_dir()
STATE_FILE = os.path.join(get_data_dir(), '.workspace_sync_state.json')
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
IGNORE_PATTERNS = {'.git', '__pycache__', 'node_modules', '.DS_Store', '.venv', '.env'}

_sync_state: dict[str, float] = {}  # file_key -> last synced mtime


def _load_state():
    """Load sync state from disk."""
    global _sync_state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                _sync_state = json.load(f)
    except Exception:
        _sync_state = {}


def _save_state():
    """Persist sync state to disk."""
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(_sync_state, f)
    except Exception:
        pass


def _get_config():
    return {
        "worker_url": os.getenv("CF_WORKER_URL", ""),
        "api_key": os.getenv("NODE_TOKEN", "") or os.getenv("CF_WORKER_API_KEY", ""),
    }


def _should_ignore(path: str) -> bool:
    parts = Path(path).parts
    return any(p in IGNORE_PATTERNS for p in parts)


def _get_content_type(filename: str) -> str:
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    types = {
        'md': 'text/markdown', 'txt': 'text/plain', 'json': 'application/json',
        'py': 'text/x-python', 'js': 'text/javascript', 'ts': 'text/typescript',
        'html': 'text/html', 'css': 'text/css', 'csv': 'text/csv',
        'yaml': 'text/yaml', 'yml': 'text/yaml', 'toml': 'text/toml',
        'sh': 'text/x-shellscript', 'rs': 'text/x-rust', 'go': 'text/x-go',
        'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
        'gif': 'image/gif', 'svg': 'image/svg+xml', 'webp': 'image/webp',
        'pdf': 'application/pdf', 'zip': 'application/zip',
    }
    return types.get(ext, 'text/plain')


async def _upload_file(client: httpx.AsyncClient, worker_url: str, api_key: str,
                       session_id: str, rel_path: str, filepath: str) -> bool:
    """Upload a single file to R2. Returns True on success."""
    try:
        file_size = os.path.getsize(filepath)
        if file_size > MAX_FILE_SIZE:
            return False

        with open(filepath, 'r', errors='replace') as f:
            content = f.read()

        resp = await client.post(
            f"{worker_url}/api/sessions/{session_id}/workspace/upload",
            json={"path": rel_path, "content": content, "contentType": _get_content_type(filepath)},
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            timeout=30,
        )
        if resp.status_code < 300:
            print(f"[workspace-sync] Uploaded: {session_id}/{rel_path}")
            return True
        return False
    except Exception as e:
        print(f"[workspace-sync] Upload failed {session_id}/{rel_path}: {e}")
        return False


def _collect_changed_files(session_dir: str, session_id: str) -> list[tuple[str, str, float]]:
    """Collect files that changed since last sync. Returns [(rel_path, filepath, mtime)]."""
    changed = []
    for root, dirs, files in os.walk(session_dir):
        dirs[:] = [d for d in dirs if d not in IGNORE_PATTERNS]
        for filename in files:
            if filename in IGNORE_PATTERNS:
                continue
            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, session_dir)
            if _should_ignore(rel_path):
                continue
            try:
                mtime = os.path.getmtime(filepath)
            except OSError:
                continue
            file_key = f"{session_id}/{rel_path}"
            last_synced = _sync_state.get(file_key, 0)
            if mtime > last_synced:
                changed.append((rel_path, filepath, mtime))
    return changed


async def _scan_and_sync():
    """Scan all sessions, upload only changed files."""
    cfg = _get_config()
    if not cfg["worker_url"] or not cfg["api_key"]:
        return

    workspaces_dir = os.path.abspath(WORKSPACES_DIR)
    if not os.path.exists(workspaces_dir):
        return

    dirty = False
    async with httpx.AsyncClient() as client:
        for session_id in os.listdir(workspaces_dir):
            session_dir = os.path.join(workspaces_dir, session_id)
            if not os.path.isdir(session_dir):
                continue

            changed = _collect_changed_files(session_dir, session_id)
            for rel_path, filepath, mtime in changed:
                ok = await _upload_file(client, cfg["worker_url"], cfg["api_key"],
                                        session_id, rel_path, filepath)
                if ok:
                    _sync_state[f"{session_id}/{rel_path}"] = mtime
                    dirty = True

    if dirty:
        _save_state()


def start_workspace_sync():
    """Run a one-time full sync on startup. No background polling."""
    _load_state()
    asyncio.create_task(_startup_sync())


async def _startup_sync():
    """One-time sync of all changed files at startup."""
    try:
        await _scan_and_sync()
        print(f"[workspace-sync] Startup sync done (tracked={len(_sync_state)} files)")
    except Exception as e:
        print(f"[workspace-sync] Startup sync error: {e}")


def stop_workspace_sync():
    _save_state()


async def sync_session_now(session_id: str):
    """Immediately sync a specific session — incremental, only changed files."""
    if not session_id:
        return
    cfg = _get_config()
    if not cfg["worker_url"] or not cfg["api_key"]:
        return

    workspaces_dir = os.path.abspath(WORKSPACES_DIR)
    session_dir = os.path.join(workspaces_dir, session_id)
    if not os.path.isdir(session_dir):
        return

    changed = _collect_changed_files(session_dir, session_id)
    if not changed:
        return

    dirty = False
    async with httpx.AsyncClient() as client:
        for rel_path, filepath, mtime in changed:
            ok = await _upload_file(client, cfg["worker_url"], cfg["api_key"],
                                    session_id, rel_path, filepath)
            if ok:
                _sync_state[f"{session_id}/{rel_path}"] = mtime
                dirty = True

    if dirty:
        _save_state()
