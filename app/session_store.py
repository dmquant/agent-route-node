"""Environment-independent Session & Project persistence.

Uses a standalone SQLite database (sessions.db) that is fully
decoupled from workspace directories or agent-specific configs.
"""

import sqlite3
import os
import time
import json
from uuid import uuid4
from typing import Dict, List, Optional

from app.config import get_db_path, get_workspaces_dir
DB_PATH = get_db_path()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def init_session_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    c = conn.cursor()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS clients (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            api_key TEXT UNIQUE NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            client_id TEXT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            color TEXT DEFAULT '#6366f1',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            client_id TEXT,
            title TEXT DEFAULT 'New Session',
            agent_type TEXT DEFAULT 'gemini',
            workspace_dir TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            image_b64 TEXT,
            agent_type TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS session_context_links (
            id TEXT PRIMARY KEY,
            source_session_id TEXT NOT NULL,
            target_session_id TEXT NOT NULL,
            link_type TEXT NOT NULL DEFAULT 'reference',
            label TEXT DEFAULT '',
            include_messages INTEGER DEFAULT 1,
            include_files INTEGER DEFAULT 1,
            max_messages INTEGER DEFAULT 50,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (source_session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (target_session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id);
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
        CREATE INDEX IF NOT EXISTS idx_context_links_source ON session_context_links(source_session_id);
        CREATE INDEX IF NOT EXISTS idx_context_links_target ON session_context_links(target_session_id);
    ''')
    conn.commit()

    # ─── Migration: add workspace_dir column if missing (existing DBs) ───
    try:
        c.execute("SELECT workspace_dir FROM sessions LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE sessions ADD COLUMN workspace_dir TEXT")
        conn.commit()
        print("[Migration] Added workspace_dir column to sessions table.")

    try:
        c.execute("SELECT client_id FROM projects LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE projects ADD COLUMN client_id TEXT")
        c.execute("ALTER TABLE sessions ADD COLUMN client_id TEXT")
        conn.commit()
        print("[Migration] Added client_id column to projects and sessions")

    conn.close()


def _get_workspace_base() -> str:
    """Return the base directory for per-session workspaces from env."""
    return os.getenv("WORKSPACE_BASE", get_workspaces_dir())


def _provision_session_workspace(session_id: str) -> str:
    """Create an isolated working directory for a session and return its path."""
    base = _get_workspace_base()
    workspace = os.path.join(base, session_id)
    os.makedirs(workspace, exist_ok=True)
    return workspace


# ─── Clients ──────────────────────────────────────────────────

def create_client(name: str) -> Dict:
    conn = _get_conn()
    cid = uuid4().hex
    api_key = f"sk_{uuid4().hex}"
    now = int(time.time() * 1000)
    conn.execute(
        'INSERT INTO clients (id, name, api_key, created_at, updated_at) VALUES (?,?,?,?,?)',
        (cid, name, api_key, now, now)
    )
    conn.commit()
    row = conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone()
    conn.close()
    return dict(row)

def get_client_by_api_key(api_key: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute('SELECT * FROM clients WHERE api_key=?', (api_key,)).fetchone()
    conn.close()
    return dict(row) if row else None

def list_clients() -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute('SELECT * FROM clients ORDER BY updated_at DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_client(client_id: str) -> bool:
    conn = _get_conn()
    conn.execute('DELETE FROM clients WHERE id=?', (client_id,))
    conn.commit()
    conn.close()
    return True

# ─── Projects ───────────────────────────────────────────────

def create_project(name: str, description: str = '', color: str = '#6366f1', client_id: Optional[str] = None) -> Dict:
    conn = _get_conn()
    pid = uuid4().hex
    now = int(time.time() * 1000)
    conn.execute(
        'INSERT INTO projects (id, client_id, name, description, color, created_at, updated_at) VALUES (?,?,?,?,?,?,?)',
        (pid, client_id, name, description, color, now, now)
    )
    conn.commit()
    row = conn.execute('SELECT * FROM projects WHERE id=?', (pid,)).fetchone()
    conn.close()
    return dict(row)

def list_projects(client_id: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    if client_id:
        rows = conn.execute('SELECT * FROM projects WHERE client_id=? ORDER BY updated_at DESC', (client_id,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM projects ORDER BY updated_at DESC').fetchall()
    result = []
    for r in rows:
        d = dict(r)
        cnt = conn.execute('SELECT COUNT(*) as c FROM sessions WHERE project_id=?', (r['id'],)).fetchone()
        d['session_count'] = cnt['c'] if cnt else 0
        result.append(d)
    conn.close()
    return result

def update_project(project_id: str, name: Optional[str] = None, description: Optional[str] = None, color: Optional[str] = None, client_id: Optional[str] = None) -> Optional[Dict]:
    conn = _get_conn()
    existing = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    now = int(time.time() * 1000)
    
    # Safely get client_id handling if existing column might not have it loaded due to PRAGMA cache
    existing_dict = dict(existing)
    existing_cid = existing_dict.get('client_id')
    
    conn.execute(
        'UPDATE projects SET name=?, description=?, color=?, client_id=?, updated_at=? WHERE id=?',
        (
            name if name is not None else existing_dict['name'],
            description if description is not None else existing_dict['description'],
            color if color is not None else existing_dict['color'],
            client_id if client_id is not None else existing_cid,
            now,
            project_id
        )
    )
    conn.commit()
    row = conn.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
    conn.close()
    return dict(row)


def delete_project(project_id: str) -> bool:
    conn = _get_conn()
    conn.execute('DELETE FROM projects WHERE id=?', (project_id,))
    conn.commit()
    conn.close()
    return True


# ─── Sessions ───────────────────────────────────────────────

def create_session(project_id: Optional[str] = None, client_id: Optional[str] = None, title: str = 'New Session', agent_type: str = 'gemini', session_id: Optional[str] = None) -> Dict:
    conn = _get_conn()
    sid = session_id or uuid4().hex
    now = int(time.time() * 1000)
    # Provision an isolated workspace directory for this session
    workspace = _provision_session_workspace(sid)
    conn.execute(
        'INSERT INTO sessions (id, project_id, client_id, title, agent_type, workspace_dir, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)',
        (sid, project_id, client_id, title, agent_type, workspace, now, now)
    )
    conn.commit()
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
    conn.close()
    return dict(row)

def list_sessions(project_id: Optional[str] = None, client_id: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    query = 'SELECT * FROM sessions'
    conditions = []
    params = []
    if project_id:
        conditions.append('project_id=?')
        params.append(project_id)
    if client_id:
        conditions.append('client_id=?')
        params.append(client_id)
        
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY updated_at DESC'
    
    rows = conn.execute(query, tuple(params)).fetchall()
    
    result = []
    for r in rows:
        d = dict(r)
        cnt = conn.execute('SELECT COUNT(*) as c FROM messages WHERE session_id=?', (r['id'],)).fetchone()
        d['message_count'] = cnt['c'] if cnt else 0
        # Get last message preview
        last = conn.execute(
            'SELECT content, source FROM messages WHERE session_id=? ORDER BY created_at DESC LIMIT 1',
            (r['id'],)
        ).fetchone()
        d['last_message'] = dict(last) if last else None
        result.append(d)
    conn.close()
    return result


def get_session(session_id: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_session(session_id: str, title: Optional[str] = None, project_id: Optional[str] = '__UNSET__', client_id: Optional[str] = '__UNSET__') -> Optional[Dict]:
    conn = _get_conn()
    existing = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    if not existing:
        conn.close()
        return None
    now = int(time.time() * 1000)
    existing_dict = dict(existing)
    
    new_title = title if title is not None else existing_dict['title']
    new_project = existing_dict['project_id'] if project_id == '__UNSET__' else project_id
    new_client = existing_dict.get('client_id') if client_id == '__UNSET__' else client_id
    
    conn.execute(
        'UPDATE sessions SET title=?, project_id=?, client_id=?, updated_at=? WHERE id=?',
        (new_title, new_project, new_client, now, session_id)
    )
    conn.commit()
    row = conn.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
    conn.close()
    return dict(row)


def delete_session(session_id: str) -> bool:
    conn = _get_conn()
    # Optionally clean up the workspace directory
    row = conn.execute('SELECT workspace_dir FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row and row['workspace_dir']:
        import shutil
        try:
            shutil.rmtree(row['workspace_dir'], ignore_errors=True)
        except Exception:
            pass
    conn.execute('DELETE FROM sessions WHERE id=?', (session_id,))
    conn.commit()
    conn.close()
    return True


def get_session_workspace(session_id: str) -> str:
    """Get the isolated workspace directory for a session.
    
    Always derived from the current SESSION_WORKSPACE_BASE env var + session_id.
    This ensures .env changes take effect immediately without stale DB paths.
    Updates the DB record if the stored path differs from the computed one.
    """
    workspace = _provision_session_workspace(session_id)
    
    # Update DB if stored path differs (env change or migration)
    conn = _get_conn()
    row = conn.execute('SELECT workspace_dir FROM sessions WHERE id=?', (session_id,)).fetchone()
    if row and row['workspace_dir'] != workspace:
        now = int(time.time() * 1000)
        conn.execute('UPDATE sessions SET workspace_dir=?, updated_at=? WHERE id=?', (workspace, now, session_id))
        conn.commit()
    conn.close()
    
    return workspace


# ─── Messages ───────────────────────────────────────────────

def add_message(session_id: str, source: str, content: str, agent_type: Optional[str] = None, image_b64: Optional[str] = None) -> Dict:
    conn = _get_conn()
    now = int(time.time() * 1000)
    c = conn.execute(
        'INSERT INTO messages (session_id, source, content, agent_type, image_b64, created_at) VALUES (?,?,?,?,?,?)',
        (session_id, source, content, agent_type, image_b64, now)
    )
    # Update session's updated_at timestamp
    conn.execute('UPDATE sessions SET updated_at=? WHERE id=?', (now, session_id))
    conn.commit()
    row = conn.execute('SELECT * FROM messages WHERE id=?', (c.lastrowid,)).fetchone()
    conn.close()
    return dict(row)


def get_messages(session_id: str, limit: int = 200, offset: int = 0) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        'SELECT id, session_id, source, content, agent_type, created_at FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?',
        (session_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_messages_with_images(session_id: str, limit: int = 200, offset: int = 0) -> List[Dict]:
    """Full message fetch including image_b64 field."""
    conn = _get_conn()
    rows = conn.execute(
        'SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?',
        (session_id, limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def auto_title_session(session_id: str):
    """Auto-generate title from first user message if title is still default."""
    conn = _get_conn()
    session = conn.execute('SELECT title FROM sessions WHERE id=?', (session_id,)).fetchone()
    if session and session['title'] == 'New Session':
        first_msg = conn.execute(
            "SELECT content FROM messages WHERE session_id=? AND source='user' ORDER BY created_at ASC LIMIT 1",
            (session_id,)
        ).fetchone()
        if first_msg:
            title = first_msg['content'][:60]
            if len(first_msg['content']) > 60:
                title += '...'
            now = int(time.time() * 1000)
            conn.execute('UPDATE sessions SET title=?, updated_at=? WHERE id=?', (title, now, session_id))
            conn.commit()
    conn.close()


# ─── Context Links ──────────────────────────────────────────

def create_context_link(
    source_session_id: str,
    target_session_id: str,
    link_type: str = 'reference',
    label: str = '',
    include_messages: bool = True,
    include_files: bool = True,
    max_messages: int = 50,
) -> Dict:
    """Create a context link between two sessions.

    Link types:
        'reference'        — Source can read target's messages/events as context
        'fork'             — Source was forked from target (inherits full history)
        'shared_workspace' — Both sessions share the same workspace directory
    """
    conn = _get_conn()
    link_id = uuid4().hex[:16]
    now = int(time.time() * 1000)

    # Validate both sessions exist
    src = conn.execute('SELECT id FROM sessions WHERE id=?', (source_session_id,)).fetchone()
    tgt = conn.execute('SELECT id FROM sessions WHERE id=?', (target_session_id,)).fetchone()
    if not src or not tgt:
        conn.close()
        return None

    # Prevent duplicate links
    existing = conn.execute(
        'SELECT id FROM session_context_links WHERE source_session_id=? AND target_session_id=?',
        (source_session_id, target_session_id),
    ).fetchone()
    if existing:
        conn.close()
        return {'id': existing['id'], 'already_exists': True}

    conn.execute(
        '''INSERT INTO session_context_links
           (id, source_session_id, target_session_id, link_type, label,
            include_messages, include_files, max_messages, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)''',
        (link_id, source_session_id, target_session_id, link_type, label,
         1 if include_messages else 0, 1 if include_files else 0, max_messages, now),
    )

    # If shared_workspace, update source to use target's workspace
    if link_type == 'shared_workspace':
        tgt_row = conn.execute('SELECT workspace_dir FROM sessions WHERE id=?', (target_session_id,)).fetchone()
        if tgt_row and tgt_row['workspace_dir']:
            conn.execute(
                'UPDATE sessions SET workspace_dir=?, updated_at=? WHERE id=?',
                (tgt_row['workspace_dir'], now, source_session_id),
            )

    conn.commit()
    row = conn.execute('SELECT * FROM session_context_links WHERE id=?', (link_id,)).fetchone()
    conn.close()
    return dict(row)


def get_context_links(session_id: str) -> List[Dict]:
    """Get all context links for a session (both incoming and outgoing)."""
    conn = _get_conn()
    # Outgoing links: sessions this session reads from
    outgoing = conn.execute(
        '''SELECT cl.*, s.title as target_title, s.agent_type as target_agent
           FROM session_context_links cl
           JOIN sessions s ON cl.target_session_id = s.id
           WHERE cl.source_session_id = ?
           ORDER BY cl.created_at DESC''',
        (session_id,),
    ).fetchall()
    # Incoming links: sessions that read from this session
    incoming = conn.execute(
        '''SELECT cl.*, s.title as source_title, s.agent_type as source_agent
           FROM session_context_links cl
           JOIN sessions s ON cl.source_session_id = s.id
           WHERE cl.target_session_id = ?
           ORDER BY cl.created_at DESC''',
        (session_id,),
    ).fetchall()
    conn.close()
    return {
        'outgoing': [dict(r) for r in outgoing],
        'incoming': [dict(r) for r in incoming],
    }


def delete_context_link(link_id: str) -> bool:
    """Remove a context link."""
    conn = _get_conn()
    conn.execute('DELETE FROM session_context_links WHERE id=?', (link_id,))
    conn.commit()
    conn.close()
    return True


def get_linked_messages(
    session_id: str,
    limit_per_link: int = 50,
) -> List[Dict]:
    """Get messages from all linked (reference) sessions.

    Returns messages from linked sessions, tagged with their source session.
    Used by the context engine to inject cross-session context.
    """
    conn = _get_conn()
    links = conn.execute(
        '''SELECT * FROM session_context_links
           WHERE source_session_id = ? AND include_messages = 1
           ORDER BY created_at ASC''',
        (session_id,),
    ).fetchall()

    all_messages = []
    for link in links:
        lim = min(link['max_messages'], limit_per_link)
        target_id = link['target_session_id']
        # Get the most recent N messages from the linked session
        msgs = conn.execute(
            '''SELECT id, session_id, source, content, agent_type, created_at
               FROM messages WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?''',
            (target_id, lim),
        ).fetchall()
        # Get session title for context
        target_session = conn.execute(
            'SELECT title FROM sessions WHERE id=?', (target_id,),
        ).fetchone()
        target_title = target_session['title'] if target_session else target_id[:8]
        for m in reversed(msgs):  # re-order chronologically
            d = dict(m)
            d['_linked_from'] = target_id
            d['_linked_title'] = target_title
            d['_link_type'] = link['link_type']
            all_messages.append(d)

    conn.close()
    return all_messages


def fork_session(
    source_session_id: str,
    title: str = '',
    agent_type: str = '',
    copy_messages: int = 0,
) -> Dict:
    """Fork a new session from an existing one.

    Creates a new session linked to the source via 'fork' link.
    Optionally copies the last N messages for immediate context.
    """
    conn = _get_conn()
    source = conn.execute('SELECT * FROM sessions WHERE id=?', (source_session_id,)).fetchone()
    if not source:
        conn.close()
        return None

    conn.close()

    new_title = title or f"Fork of {source['title']}"
    new_agent = agent_type or source['agent_type']
    new_session = create_session(
        project_id=source['project_id'],
        title=new_title,
        agent_type=new_agent,
    )

    # Create fork link
    create_context_link(
        source_session_id=new_session['id'],
        target_session_id=source_session_id,
        link_type='fork',
        label=f"Forked from {source['title']}",
    )

    # Optionally copy messages
    if copy_messages > 0:
        conn = _get_conn()
        msgs = conn.execute(
            '''SELECT source, content, agent_type, image_b64
               FROM messages WHERE session_id = ?
               ORDER BY created_at DESC LIMIT ?''',
            (source_session_id, copy_messages),
        ).fetchall()
        now = int(time.time() * 1000)
        for m in reversed(msgs):
            conn.execute(
                '''INSERT INTO messages (session_id, source, content, agent_type, image_b64, created_at)
                   VALUES (?,?,?,?,?,?)''',
                (new_session['id'], m['source'], m['content'], m['agent_type'], m['image_b64'], now),
            )
            now += 1  # ensure ordering
        conn.commit()
        conn.close()

    return new_session
