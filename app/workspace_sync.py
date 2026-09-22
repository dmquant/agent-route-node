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
# One detector, two sites: the upload refuses to publish a saved refusal and the
# staging manifest marks one that is already stored. Sharing the function is the
# point -- two spellings of "is this a refusal?" would drift, and the one that
# drifted would be the one nobody was watching.
from app.institute_files import refusal_envelope
WORKSPACES_DIR = get_workspaces_dir()
STATE_FILE = os.path.join(get_data_dir(), '.workspace_sync_state.json')
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
IGNORE_PATTERNS = {'.git', '__pycache__', 'node_modules', '.DS_Store', '.venv', '.env', '.antigravitycli',
                   # Written by app/mcp_config.py into the workspace. Node-local
                   # CLI configuration, not task output -- and belt-and-braces
                   # against ever shipping a credential to R2, even though the
                   # file carries an env reference rather than the value.
                   'opencode.json', '.mcp.json'}

_sync_state: dict[str, float] = {}  # file_key -> last synced mtime
# Non-empty when the state file EXISTS but could not be parsed; see _load_state.
_state_was_unreadable = ""


def _load_state():
    """Load sync state from disk."""
    global _sync_state, _state_was_unreadable
    # NO STATE FILE and UNREADABLE STATE FILE are different facts with the same
    # consequence, and only one of them is normal.
    #
    # Both used to land on {} silently. An unreadable state file then reads as
    # "nothing has ever been synced", so the next pass re-uploads every file in
    # every workspace -- 97,815 of them on gubei12 as of 2026-09-21 -- and the
    # logs look exactly like a legitimate first run.
    #
    # The empty dict still has to stand, because there is nothing else to
    # proceed with. What changes is that the caller and the log can tell which
    # case they are in.
    if not os.path.exists(STATE_FILE):
        _sync_state = {}
        return
    try:
        with open(STATE_FILE, 'r') as f:
            _sync_state = json.load(f)
        _state_was_unreadable = ""
    except Exception as e:
        _sync_state = {}
        _state_was_unreadable = "%s: %s" % (STATE_FILE, repr(e)[:120])
        print("[workspace-sync] ⚠ STATE FILE UNREADABLE — %s. Treating this as a "
              "FIRST SYNC, which will re-upload every tracked file. This is NOT "
              "the same as having no state, and the file was not deleted."
              % _state_was_unreadable)


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

        # DO NOT UPLOAD A SAVED REFUSAL INTO THE SHARED STORE.
        #
        # This is where the operator's 2026-09-21 defect actually enters the
        # federation. Something in a workspace fetched a URL with no credential,
        # got a 401 JSON body, and wrote it to `recent_reports.md`. This
        # function then walks the session directory, finds a changed file, and
        # uploads its bytes as the artifact. The institute stores it; a later
        # task stages it back with status ok and 236 bytes; the hand reads an
        # auth error where a report should be.
        #
        # The node cannot stop a hand writing a bad file into its own workspace.
        # It CAN decline to publish one, and this is the last point where the
        # bytes are still local.
        #
        # Refusing is the smaller harm here: an unshared file is still on disk
        # in the workspace, while a shared one becomes somebody else's input and
        # carries a byte count that makes it look like a document.
        #
        # AND THE REFUSAL IS PRINTED WHERE THE SUCCESSES ARE PRINTED. A silent
        # skip is an absence nobody can inspect, which is the defect one layer
        # up from the one being fixed.
        # A BODY THAT CONTRADICTS WHAT ITS NAME PROMISED.
        #
        # rho found 6 of 84 `.pdf` locators holding something else, and FIVE of
        # the six declared nothing wrong at all -- a bot challenge, an app
        # shell, two gzip WAF pages. No absence to detect and no false claim to
        # catch: they are simply not the document. The only signal is the
        # mismatch with the REQUEST, which no guard reading the body can see.
        #
        # Measured here after their question: 70 files across the two nodes are
        # named .csv/.json/.txt and hold HTML. The sharpest pair is CPI.csv at
        # 28,898 bytes on one node and THA.csv at 28,897 on the other -- the
        # same upstream error page, fetched for two DIFFERENT series. A size
        # rule passes 28 KB comfortably and the refusal detector cannot see it,
        # because an error PAGE is not an error ENVELOPE.
        #
        # THIS MARKS AND DOES NOT REFUSE, which is the opposite of the refusal
        # guard above, and the asymmetry is ai-institute's: a saved refusal is
        # never the document, but a scraped HTML page saved as .txt may be
        # exactly what the task wanted. 99,554 bytes of a Sina Finance filing
        # sits in this population and is not a defect. Blocking it would
        # destroy research to catch a naming mismatch.
        mismatch = locator_contradiction(rel_path, content)
        if mismatch:
            print(f"[workspace-sync] ⚠ {session_id}/{rel_path}: {mismatch}")

        refusal = refusal_envelope(content)
        if refusal:
            print(f"[workspace-sync] REFUSED {session_id}/{rel_path}: {refusal}")
            _leave_refusal_marker(filepath, rel_path, refusal)
            return False

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


# Extensions that promise a machine-readable shape. A mismatch here is worth
# saying out loud; a mismatch on .md or .html is not, because prose and markup
# legitimately contain anything.
_PROMISES_DATA = (".csv", ".json", ".tsv", ".xml", ".yaml", ".yml")


def locator_contradiction(rel_path: str, content: str) -> str:
    """One line if the body contradicts what the filename promised, else "".

    Deliberately narrow: only HTML found where a DATA extension was named. That
    is the case with no benign reading -- nothing legitimately writes a web page
    into a .csv -- and it is where the measured population sits.
    """
    # SIZES HERE ARE CHARACTERS, NOT BYTES, AND THE MESSAGE SAYS SO.
    #
    # `content` is already decoded, so len() counts CHARACTERS. Measured
    # 2026-09-22: I published CPI.csv at "28,898 bytes" when the file is 28,936
    # bytes -- 38 short, because the page carries multi-byte UTF-8. Worse, I
    # made "the two files are within ONE BYTE of each other" the centrepiece of
    # a finding, and both figures were wrong by the same 38, so the difference
    # survived by accident and described my decoder rather than the pages.
    #
    # Same class as a MiB value labelled MB: a magnitude reported in a unit it
    # was not measured in. The caller has the decoded string and cannot recover
    # the byte count from it, so the honest fix is to NAME the unit correctly
    # rather than to guess at bytes.
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in _PROMISES_DATA:
        return ""
    head = content.lstrip()[:300].lower()
    if not (head.startswith("<!doctype html") or head.startswith("<html")
            or "<head>" in head):
        return ""
    import re as _re
    m = _re.search(r"<title[^>]*>(.*?)</title>", content[:4000], _re.I | _re.S)
    title = m.group(1).strip()[:80] if m else ""
    return ("named %s but the bytes are an HTML PAGE (%d chars%s). The fetch "
            "returned a web page where data was expected -- do not parse this "
            "as %s, and treat the data as MISSING."
            % (ext, len(content),
               ", titled %r" % title if title else ", no title at all",
               ext.lstrip(".")))


def _leave_refusal_marker(filepath: str, rel_path: str, refusal: str) -> None:
    """Tell the HAND why its file did not publish, where the hand will find it.

    ai-institute asked the right question about the first version of this guard:
    does anything tell the hand why its file did not publish, or does the file
    simply not appear? It simply did not appear. The refusal went to the node's
    stdout, which the model that wrote the file never reads.

    That is the same defect the guard exists to fix, one layer up -- a thing
    that is missing, with nothing at the place you would look to say why. A
    node that declines to publish and says so only to itself has moved the
    uninspectable absence rather than removed it.

    So the marker is a SIBLING FILE in the workspace: the hand looks for its
    output where it wrote it, and finds the reason sitting next to it. It is
    plain prose, so it publishes normally on the next pass -- which means the
    session's stored record also carries the explanation, rather than a gap.

    Never raises: a marker that breaks the sync would be a worse bug than the
    one it reports.
    """
    try:
        with open(filepath + ".NOT_PUBLISHED.md", "w", encoding="utf-8") as fh:
            fh.write(
                "# %s was NOT published to the session store\n\n"
                "This node declined to upload it. The file is still on disk here,\n"
                "unchanged -- nothing was deleted.\n\n"
                "## Why\n\n%s\n\n"
                "## What this means for you\n\n"
                "The bytes in that file are a service refusal, not the document the\n"
                "filename promises. Whatever produced it made a request that was\n"
                "denied, and saved the denial instead of the result.\n\n"
                "Do NOT summarise, quote, or reason from its contents as though they\n"
                "were the document. Report the document as MISSING, and say that a\n"
                "refusal was saved in its place -- that sentence is the useful output\n"
                "here, and it is more useful than anything the file appears to contain.\n"
                % (rel_path, refusal))
    except Exception as e:                       # pragma: no cover - never fatal
        print(f"[workspace-sync] could not leave a refusal marker for {rel_path}: {e}")


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
