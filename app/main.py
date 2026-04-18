import os
import json
import uuid
import asyncio
import time as _time
from datetime import datetime
from typing import Optional, List, Dict
from dotenv import load_dotenv

# Boot Environment Configurations BEFORE importing modules that depend on env vars
from app.config import get_env_path, get_workspaces_dir
load_dotenv(dotenv_path=get_env_path())

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
import websockets
import httpx

# Legacy executor.py and history.py removed — all execution flows through Hand protocol
# and SessionEventManager now. Historical logs migrated to session events.
from app.agent_registry import get_all_agents, discover_skills
from app.report_engine import get_daily_stats, build_report_prompt
from app.report_store import (
    init_report_tables, save_report, list_reports, get_report,
    get_report_by_date, update_report, delete_report,
)
from app.hands.registry import hand_registry, auto_register_all
from app.session.manager import session_events, init_event_tables
from app.session.events import EventType
from app.brain.orchestrator import orchestrator
from app.brain.harness import harness_manager
from app.sandbox.pool import sandbox_pool, init_sandbox_tables
from app.tasks import task_manager, TaskPhase, init_task_tables, get_task_history
from app.task_analytics import get_task_analytics, get_benchmark_comparison
from app.workflow_store import (
    init_workflow_tables, create_workflow, list_workflows, get_workflow,
    update_workflow, delete_workflow, create_run, update_run, get_run, list_runs,
)
from app.workflow_executor import workflow_executor
from app.scheduler import start_scheduler, add_cron_job, list_jobs, remove_job
from app.session_store import (
    init_session_db,
    create_project, list_projects, update_project, delete_project, get_client_by_api_key,
    create_client, list_clients, delete_client,
    create_session, list_sessions, get_session, update_session, delete_session,
    add_message, get_messages, get_messages_with_images, auto_title_session,
    get_session_workspace,
    create_context_link, get_context_links, delete_context_link,
    get_linked_messages, fork_session,
)
from app.api_logger import (
    init_api_log_tables, record_api_call, get_api_calls,
    get_api_stats, get_api_timeline, _extract_agent,
)
from app.edge_register import register_with_worker, start_heartbeat, stop_heartbeat
from app.workspace_sync import start_workspace_sync, stop_workspace_sync, sync_session_now

app = FastAPI(title="AI Execution Bridge API", description="Pydantic structured REST & WS gateway for Agent CLIs.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Auth Dependency ──────────────────────────────────────────

async def get_current_client(x_api_key: Optional[str] = Header(None)) -> Optional[Dict]:
    """
    X-API-Key is strictly required. 
    If it matches ADMIN_API_KEY, it returns None, giving full admin access explicitly.
    Otherwise we validate it against clients table.
    """
    admin_key = os.getenv("ADMIN_API_KEY", "sk_admin_route_2025")
    
    if not x_api_key:
        raise HTTPException(status_code=401, detail="API Key is required")
    
    if x_api_key == admin_key:
        return None  # Admin explicit full access
        
    client = get_client_by_api_key(x_api_key)
    if not client:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return client

# ─── API Call Logging Middleware ──────────────────────────────
# Records every HTTP request for the unified activity feed.
# WebSocket upgrades are excluded (they log at connection time).

_SKIP_LOG_PREFIXES = ("/docs", "/openapi.json", "/favicon")

class APICallLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Skip internal/docs paths
        if any(path.startswith(p) for p in _SKIP_LOG_PREFIXES):
            return await call_next(request)

        request_id = uuid.uuid4().hex[:16]
        method = request.method
        query = str(request.url.query) if request.url.query else ""
        client_ip = request.client.host if request.client else ""
        user_agent = request.headers.get("user-agent", "")[:200]

        # Try to read body preview for POST/PUT (non-blocking)
        body_preview = ""
        agent = ""
        if method in ("POST", "PUT"):
            try:
                body_bytes = await request.body()
                body_preview = body_bytes.decode("utf-8", errors="replace")[:500]
                try:
                    body_json = json.loads(body_bytes)
                    agent = _extract_agent(path, body_json)
                except (json.JSONDecodeError, Exception):
                    pass
            except Exception:
                pass

        # Detect source: UI frontend vs external API client
        source = "ui" if ("localhost:5173" in request.headers.get("origin", "")
                          or "localhost:5173" in request.headers.get("referer", "")
                          ) else "api"

        start = _time.monotonic()
        response = await call_next(request)
        duration_ms = (_time.monotonic() - start) * 1000

        # Record asynchronously to avoid blocking the response
        try:
            record_api_call(
                request_id=request_id,
                method=method,
                path=path,
                query_params=query,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
                client_ip=client_ip,
                user_agent=user_agent,
                request_body_preview=body_preview,
                agent=agent,
                source=source,
            )
        except Exception:
            pass  # Never let logging break the request

        return response

app.add_middleware(APICallLogMiddleware)

class ExecutionRequest(BaseModel):
    client: str
    prompt: str
    workspace_id: Optional[str] = None
    node_id: Optional[str] = "api_request"
    role: Optional[str] = "system"
    model: Optional[str] = None

class ExecutionResponse(BaseModel):
    exitCode: Optional[int]
    output: str

# ─── Pydantic models for session/project API ────────────────

class ClientCreate(BaseModel):
    name: str

class ProjectCreate(BaseModel):
    name: str
    description: str = ''
    color: str = '#6366f1'
    client_id: Optional[str] = None

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    client_id: Optional[str] = None

class SessionCreate(BaseModel):
    project_id: Optional[str] = None
    title: str = 'New Session'
    agent_type: str = 'gemini'
    client_id: Optional[str] = None

class SessionUpdate(BaseModel):
    title: Optional[str] = None
    project_id: Optional[str] = None
    client_id: Optional[str] = None


# ---------------------------------------------
# 1. Native Model Discovery & Configuration
# ---------------------------------------------
@app.on_event("startup")
async def startup_event():
    init_session_db()
    init_event_tables()
    init_sandbox_tables()
    init_task_tables()
    init_report_tables()
    init_workflow_tables()
    init_api_log_tables()
    # Register all execution hands (Managed Agents Phase 1)
    auto_register_all()
    print(f"[Startup] Hand Registry: {hand_registry.list_names()}")
    start_scheduler()
    # Start periodic GC for completed tasks
    await task_manager.start_gc_loop(interval_seconds=60, max_age_ms=300000)
    # Register with CF Worker as edge node (if configured)
    available = [{"name": name} for name in hand_registry.list_names()]
    await register_with_worker(available)
    start_heartbeat()
    start_workspace_sync()

@app.on_event("shutdown")
async def shutdown_event():
    """Graceful shutdown: cancel running tasks and persist final state."""
    stop_workspace_sync()
    stop_heartbeat()
    task_manager.stop_gc_loop()
    await task_manager.shutdown()

@app.get("/api/logs")
def api_get_logs():
    """Legacy log endpoint — returns recent session events instead."""
    recent = session_events.get_recent_events(limit=100)
    return {"logs": recent}

@app.get("/models/ollama")
async def get_ollama_models():
    """Queries localhost:11434 for locally installed models"""
    if os.getenv("ENABLE_OLLAMA_API") != "true":
        return {"models": []}
        
    try:
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ollama_url}/api/tags", timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                return {"models": [m["name"] for m in data.get("models", [])]}
    except Exception as e:
         print(f"Ollama discovery failed: {e}")
         return {"models": []}


# ---------------------------------------------
# 2. PROJECT & SESSION CRUD API
# ---------------------------------------------

@app.get("/api/clients")
def api_list_clients():
    return {"clients": list_clients()}

@app.post("/api/clients")
def api_create_client(body: ClientCreate):
    client = create_client(name=body.name)
    return {"client": client}

@app.delete("/api/clients/{client_id}")
def api_delete_client(client_id: str):
    delete_client(client_id)
    return {"ok": True}

@app.get("/api/projects")
def api_list_projects():
    return {"projects": list_projects()}

@app.post("/api/projects")
def api_create_project(body: ProjectCreate, current_client: Optional[Dict] = Depends(get_current_client)):
    # Standard clients implicitly own their creations. Admin skips this to explicitly assign.
    cid = current_client["id"] if current_client else body.client_id
    project = create_project(name=body.name, description=body.description, color=body.color, client_id=cid)
    return {"project": project}

@app.put("/api/projects/{project_id}")
def api_update_project(project_id: str, body: ProjectUpdate):
    project = update_project(project_id, name=body.name, description=body.description, color=body.color, client_id=body.client_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"project": project}

@app.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    delete_project(project_id)
    return {"ok": True}

@app.get("/api/sessions")
def api_list_sessions(project_id: Optional[str] = None, current_client: Optional[Dict] = Depends(get_current_client)):
    cid = current_client["id"] if current_client else None
    return {"sessions": list_sessions(project_id=project_id, client_id=cid)}

@app.post("/api/sessions")
def api_create_session(body: SessionCreate, current_client: Optional[Dict] = Depends(get_current_client)):
    # Standard clients implicitly own their creations. Admin skips this to explicitly assign.
    cid = current_client["id"] if current_client else body.client_id
    session = create_session(project_id=body.project_id, client_id=cid, title=body.title, agent_type=body.agent_type)
    return {"session": session}

@app.get("/api/sessions/{session_id}")
def api_get_session(session_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    cid = current_client["id"] if current_client else None
    if cid and session.get("client_id") != cid:
        raise HTTPException(status_code=403, detail="Access denied")
    return {"session": session}

@app.put("/api/sessions/{session_id}")
def api_update_session(session_id: str, body: SessionUpdate, current_client: Optional[Dict] = Depends(get_current_client)):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
        
    cid = current_client["id"] if current_client else None
    if cid and session.get("client_id") != cid:
        raise HTTPException(status_code=403, detail="Access denied")
        
    proj_id = '__UNSET__'
    if body.project_id is not None:
        proj_id = body.project_id if body.project_id != '' else None
        
    c_id = '__UNSET__'
    if body.client_id is not None:
        c_id = body.client_id if body.client_id != '' else None
    
    session = update_session(session_id, title=body.title, project_id=proj_id, client_id=c_id)
    return {"session": session}

@app.delete("/api/sessions/{session_id}")
def api_delete_session(session_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    cid = current_client["id"] if current_client else None
    if cid and session.get("client_id") != cid:
        raise HTTPException(status_code=403, detail="Access denied")
        
    delete_session(session_id)
    return {"ok": True}

@app.get("/api/sessions/{session_id}/messages")
def api_get_messages(session_id: str, include_images: bool = False):
    if include_images:
        msgs = get_messages_with_images(session_id)
    else:
        msgs = get_messages(session_id)
    return {"messages": msgs}

@app.get("/api/sessions/{session_id}/workspace")
def api_get_workspace_files(session_id: str, path: str = ""):
    """List files and directories in a session's workspace.
    
    Returns a tree structure with name, type (file/dir), size, and children.
    The optional `path` query param navigates into subdirectories.
    """
    workspace = get_session_workspace(session_id)
    target = os.path.join(workspace, path) if path else workspace
    target = os.path.realpath(target)
    
    # Security: prevent path traversal outside workspace
    if not target.startswith(os.path.realpath(workspace)):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    
    if not os.path.exists(target):
        return {"files": [], "workspace_dir": workspace}
    
    entries = []
    try:
        for entry in sorted(os.listdir(target)):
            if entry.startswith('.'):
                continue  # Skip hidden files (.git, .DS_Store, etc.)
            full = os.path.join(target, entry)
            rel = os.path.relpath(full, workspace)
            if os.path.isdir(full):
                # Count children (non-hidden)
                try:
                    children_count = len([c for c in os.listdir(full) if not c.startswith('.')])
                except OSError:
                    children_count = 0
                entries.append({
                    "name": entry,
                    "path": rel,
                    "type": "directory",
                    "children_count": children_count,
                })
            else:
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                entries.append({
                    "name": entry,
                    "path": rel,
                    "type": "file",
                    "size": size,
                    "extension": os.path.splitext(entry)[1].lstrip('.'),
                })
    except OSError:
        pass
    
    return {"files": entries, "workspace_dir": workspace}

@app.get("/api/sessions/{session_id}/workspace/read")
def api_read_workspace_file(session_id: str, path: str):
    """Read the contents of a file in a session's workspace."""
    workspace = get_session_workspace(session_id)
    target = os.path.join(workspace, path)
    target = os.path.realpath(target)
    
    if not target.startswith(os.path.realpath(workspace)):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")
    
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")
    
    try:
        size = os.path.getsize(target)
        if size > 512_000:  # 500KB limit
            return {"content": None, "truncated": True, "size": size, "path": path}
        with open(target, 'r', errors='replace') as f:
            content = f.read()
        return {"content": content, "truncated": False, "size": size, "path": path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from fastapi.responses import FileResponse
import mimetypes

@app.get("/api/sessions/{session_id}/workspace/download")
def api_download_workspace_file(session_id: str, path: str):
    """Download a file from a session's workspace as binary."""
    workspace = get_session_workspace(session_id)
    target = os.path.join(workspace, path)
    target = os.path.realpath(target)

    if not target.startswith(os.path.realpath(workspace)):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found")

    filename = os.path.basename(target)
    media_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return FileResponse(
        path=target,
        filename=filename,
        media_type=media_type,
    )

@app.delete("/api/sessions/{session_id}/workspace/file")
def api_delete_workspace_file(session_id: str, path: str):
    """Delete a file from a session's workspace."""
    workspace = get_session_workspace(session_id)
    target = os.path.join(workspace, path)
    target = os.path.realpath(target)

    if not target.startswith(os.path.realpath(workspace)):
        raise HTTPException(status_code=403, detail="Path traversal not allowed")

    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail="File not found")

    import shutil
    try:
        if os.path.isdir(target):
            shutil.rmtree(target)
        else:
            os.remove(target)
        return {"deleted": True, "path": path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------------------------------------------
# 3a. Agent & Skills Discovery
# ---------------------------------------------
@app.get("/api/agents")
def api_agents():
    """Return full agent registry with discovered skills."""
    return {"agents": get_all_agents()}

@app.get("/api/agents/{agent_id}/skills")
def api_agent_skills(agent_id: str):
    """Return skills for a specific agent."""
    skills = discover_skills(agent_id)
    return {"agent": agent_id, "skills": skills}

# ---------------------------------------------
# 3b. Hand Registry Endpoints (Managed Agents)
# ---------------------------------------------
@app.get("/api/hands")
def api_list_hands():
    """List all registered execution hands."""
    return {"hands": hand_registry.list_info()}

@app.get("/api/hands/health")
async def api_hands_health():
    """Health check all registered hands."""
    status = await hand_registry.health_check_all()
    return {"health": status}

# ---------------------------------------------
# 3c. Session Events API (Managed Agents Phase 2)
# ---------------------------------------------
@app.get("/api/sessions/{session_id}/events")
def api_session_events(
    session_id: str,
    start: int = 0,
    end: int = -1,
    event_type: Optional[str] = None,
    limit: int = 200,
):
    """Interrogate the session event log with positional slicing."""
    type_filter = [event_type] if event_type else None
    events = session_events.get_events(
        session_id, start=start, end=end,
        event_types=type_filter, limit=limit,
    )
    return {"session_id": session_id, "events": [e.to_dict() for e in events]}

@app.post("/api/sessions/{session_id}/wake")
def api_session_wake(session_id: str):
    """Resume a session — brain crash recovery via event log replay."""
    result = session_events.wake(session_id)
    return result

@app.post("/api/sessions/{session_id}/checkpoint")
def api_session_checkpoint(session_id: str, summary: str = ""):
    """Save a context checkpoint for recovery."""
    checkpoint_id = session_events.checkpoint(session_id, summary)
    return {"checkpoint_id": checkpoint_id, "session_id": session_id}

@app.get("/api/sessions/{session_id}/summary")
def api_session_summary(session_id: str):
    """High-level summary of a session's event log."""
    summary = session_events.get_session_summary(session_id)
    tokens = session_events.get_token_usage(session_id)
    return {**summary, "tokens": tokens}

# ---------------------------------------------
# 3d. Brain Orchestrator API (Managed Agents Phase 3)
# ---------------------------------------------
class BrainRunRequest(BaseModel):
    agent: str
    prompt: str
    workspace_dir: Optional[str] = None
    model: Optional[str] = None

@app.post("/api/brain/{session_id}/run")
async def api_brain_run(session_id: str, req: BrainRunRequest):
    """Execute a single turn through the stateless Brain."""
    workspace_dir = req.workspace_dir or get_session_workspace(session_id)
    kwargs = {}
    if req.model:
        kwargs["model"] = req.model
    result = await orchestrator.run(
        session_id, req.agent, req.prompt,
        workspace_dir=workspace_dir, **kwargs,
    )
    return result

@app.post("/api/brain/{session_id}/wake")
def api_brain_wake(session_id: str):
    """Wake the brain for a session — rebuild state from event log."""
    return orchestrator.wake(session_id)

@app.post("/api/brain/{session_id}/pause")
def api_brain_pause(session_id: str, summary: str = ""):
    """Pause the brain — save checkpoint and yield."""
    checkpoint_id = orchestrator.pause(session_id, summary)
    return {"checkpoint_id": checkpoint_id, "status": "paused"}

class DelegateRequest(BaseModel):
    from_agent: str
    to_agent: str
    prompt: str
    workspace_dir: Optional[str] = None

@app.post("/api/brain/{session_id}/delegate")
async def api_brain_delegate(session_id: str, req: DelegateRequest):
    """Delegate execution from one agent to another."""
    workspace_dir = req.workspace_dir or get_session_workspace(session_id)
    result = await orchestrator.delegate(
        session_id, req.from_agent, req.to_agent, req.prompt,
        workspace_dir=workspace_dir,
    )
    return result

# --- Multi-Agent Delegation Endpoints (Phase 9) ---

class FanOutRequest(BaseModel):
    agents: List[str]
    prompt: str
    workspace_dir: Optional[str] = None
    timeout: float = 300.0

@app.post("/api/brain/{session_id}/fan-out")
async def api_brain_fan_out(session_id: str, req: FanOutRequest):
    """Dispatch the same prompt to multiple agents in parallel.
    
    Each agent gets its own isolated sub-workspace (_fanout_{agent}).
    Returns a list of results, one per agent, in the same order.
    """
    workspace_dir = req.workspace_dir or get_session_workspace(session_id)
    results = await orchestrator.fan_out(
        session_id, req.agents, req.prompt,
        workspace_dir=workspace_dir,
        timeout=req.timeout,
    )
    return {
        "session_id": session_id,
        "agents": req.agents,
        "results": results,
        "success_count": sum(1 for r in results if r.get("success")),
        "total": len(results),
    }

class MultiAgentRequest(BaseModel):
    agents: List[str]
    prompt: str
    strategy: str = "first_success"  # all | first_success | majority_vote | best_effort
    workspace_dir: Optional[str] = None
    timeout: float = 300.0

@app.post("/api/brain/{session_id}/multi-agent")
async def api_brain_multi_agent(session_id: str, req: MultiAgentRequest):
    """Fan-out to multiple agents, then join results with a merge strategy.
    
    Strategies:
    - all: Return all results
    - first_success: Return the first agent that succeeds
    - majority_vote: Majority-wins by exit code
    - best_effort: Return successful results, fallback to any
    """
    workspace_dir = req.workspace_dir or get_session_workspace(session_id)
    merged = await orchestrator.multi_agent_run(
        session_id, req.agents, req.prompt,
        workspace_dir=workspace_dir,
        strategy=req.strategy,
        timeout=req.timeout,
    )
    return {
        "session_id": session_id,
        "agents": req.agents,
        "strategy": req.strategy,
        **merged,
    }

@app.get("/api/brain/{session_id}/status")
def api_brain_status(session_id: str):
    """Get orchestrator status for a session."""
    return orchestrator.get_brain_status(session_id)

# --- Context Engine Endpoints ---
@app.get("/api/brain/{session_id}/context")
def api_brain_context(session_id: str, agent: str = "gemini"):
    """Build context window for a session using the specified agent's harness."""
    harness = harness_manager.select(agent)
    result = orchestrator.context.build_context(session_id, harness)
    return result

@app.get("/api/brain/{session_id}/context/stats")
def api_brain_context_stats(session_id: str, agent: str = "gemini"):
    """Get context utilization stats."""
    harness = harness_manager.select(agent)
    return orchestrator.context.get_context_stats(session_id, harness)

@app.get("/api/brain/{session_id}/context/shared")
def api_brain_shared_context(session_id: str, agent: str = "gemini"):
    """Build context window enriched with linked session context.

    Merges the current session's events with messages from linked sessions,
    respecting token budgets. Used by Brain Inspector to visualize
    cross-session context lineage.
    """
    harness = harness_manager.select(agent)
    result = orchestrator.context.build_shared_context(session_id, harness)
    return result

@app.get("/api/brain/{session_id}/context/rewind")
def api_brain_rewind(session_id: str, before_event_id: int, count: int = 10):
    """Rewind: get events leading up to a specific event."""
    return {"events": orchestrator.context.rewind(session_id, before_event_id, count)}

# --- Harness Config Endpoints ---
@app.get("/api/harnesses")
def api_list_harnesses():
    """List all harness configurations."""
    return {"harnesses": harness_manager.list_configs()}

@app.get("/api/harnesses/{agent}")
def api_get_harness(agent: str):
    """Get harness configuration for a specific agent."""
    return harness_manager.select(agent).to_dict()

# ---------------------------------------------
# 3e. Sandbox Pool API (Managed Agents Phase 5)
# ---------------------------------------------
class SandboxProvisionRequest(BaseModel):
    session_id: Optional[str] = None
    name: Optional[str] = None
    ttl_seconds: int = 86400

@app.post("/api/sandboxes")
def api_provision_sandbox(req: SandboxProvisionRequest):
    """Provision a new sandbox workspace."""
    sandbox = sandbox_pool.provision(
        session_id=req.session_id, name=req.name, ttl_seconds=req.ttl_seconds,
    )
    return sandbox.to_dict()

@app.get("/api/sandboxes")
def api_list_sandboxes():
    """List all active sandboxes."""
    return {"sandboxes": [s.to_dict() for s in sandbox_pool.list_active()]}

@app.get("/api/sandboxes/stats")
def api_sandbox_stats():
    """Get sandbox pool utilization stats."""
    return sandbox_pool.get_stats()

@app.delete("/api/sandboxes/{sandbox_id}")
def api_destroy_sandbox(sandbox_id: str):
    """Destroy a sandbox workspace."""
    success = sandbox_pool.destroy(sandbox_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Sandbox {sandbox_id} not found")
    return {"destroyed": sandbox_id}

@app.post("/api/sandboxes/gc")
def api_sandbox_gc():
    """Garbage-collect expired sandboxes."""
    destroyed = sandbox_pool.gc()
    return {"destroyed": destroyed, "count": len(destroyed)}

# ---------------------------------------------
# 3c. Daily Report Endpoints
# ---------------------------------------------
@app.get("/api/reports/daily")
def api_daily_report(date: Optional[str] = None, days: int = 1):
    """Get aggregated daily usage statistics."""
    stats = get_daily_stats(date, days)
    return stats

class ReportGenerateRequest(BaseModel):
    date: Optional[str] = None
    days: int = 1
    agent: str = "gemini"  # which agent to use for generation

@app.post("/api/reports/generate")
async def api_generate_report(req: ReportGenerateRequest):
    """Generate an AI narrative report using a selected agent, then persist it."""
    stats = get_daily_stats(req.date, req.days)
    prompt = build_report_prompt(stats)
    
    # Execute via Hand Registry
    try:
        hand = hand_registry.get(req.agent)
        if not hand:
            raise HTTPException(status_code=404, detail=f"No hand registered for '{req.agent}'")

        result = await hand.execute(prompt, workspace_dir="/tmp/reports")
        report_content = result.output or ""

        # Auto-persist the generated report
        saved = save_report(
            date=req.date or datetime.now().strftime('%Y-%m-%d'),
            days=req.days,
            agent=req.agent,
            content=report_content,
            stats=stats,
            prompt=prompt,
        )

        return {
            "report": report_content,
            "report_id": saved["id"],
            "stats": stats,
            "agent_used": req.agent,
            "prompt_length": len(prompt),
            "saved": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {str(e)}")


# ─── Report CRUD Endpoints ──────────────────────

@app.get("/api/reports")
def api_list_reports(limit: int = 50, date: Optional[str] = None, agent: Optional[str] = None):
    """List saved reports with metadata (no full content)."""
    return {"reports": list_reports(limit=limit, date=date, agent=agent)}


@app.get("/api/reports/{report_id}")
def api_get_report(report_id: str):
    """Get a single report by ID including full content."""
    report = get_report(report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


@app.get("/api/reports/date/{date}")
def api_get_report_by_date(date: str):
    """Get the most recent report generated for a specific date."""
    report = get_report_by_date(date)
    if not report:
        return {"found": False, "date": date}
    return {"found": True, **report}


class ReportUpdateRequest(BaseModel):
    title: Optional[str] = None
    pinned: Optional[bool] = None
    content: Optional[str] = None


@app.patch("/api/reports/{report_id}")
def api_update_report(report_id: str, req: ReportUpdateRequest):
    """Update report metadata (title, pinned status, or content)."""
    updates = {}
    if req.title is not None:
        updates["title"] = req.title
    if req.pinned is not None:
        updates["pinned"] = 1 if req.pinned else 0
    if req.content is not None:
        updates["content"] = req.content

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    success = update_report(report_id, **updates)
    if not success:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"updated": True}


@app.delete("/api/reports/{report_id}")
def api_delete_report(report_id: str):
    """Delete a saved report."""
    success = delete_report(report_id)
    if not success:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"deleted": True}

# ---------------------------------------------
# 3d. Workflow CRUD + Execution Endpoints
# ---------------------------------------------

class WorkflowCreateRequest(BaseModel):
    name: str
    description: str = ""
    steps: list = []
    config: dict = {}
    variables: list = []  # [{name, label, type, default, required}]
    edges: list = []      # DAG edges [{id, source, sourceHandle, target, targetHandle, condition?}]
    positions: dict = {}  # Node positions {step_id: {x, y}}
    client_id: Optional[str] = None


class WorkflowUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    steps: Optional[list] = None
    config: Optional[dict] = None
    variables: Optional[list] = None
    edges: Optional[list] = None
    positions: Optional[dict] = None
    client_id: Optional[str] = None


class WorkflowInputFile(BaseModel):
    filename: str
    content_b64: Optional[str] = None  # base64-encoded file content
    content_text: Optional[str] = None  # plain text content (alternative to b64)


class WorkflowRunRequest(BaseModel):
    session_id: Optional[str] = None
    session_title: Optional[str] = None
    input_prompt: Optional[str] = None  # Additional prompt injected into first step
    input_files: Optional[List[WorkflowInputFile]] = None  # Files written to workspace before run
    variables: Optional[Dict[str, str]] = None  # Variable values: {"TICKER": "AAPL", "DATE": "2026-04-12"}


class ScheduleJobRequest(BaseModel):
    workflow_id: str
    cron_expr: str
    input_prompt: Optional[str] = None


@app.get("/api/workflows")
def api_list_workflows(current_client: Optional[Dict] = Depends(get_current_client)):
    cid = current_client["id"] if current_client else None
    return {"workflows": list_workflows(client_id=cid)}


@app.post("/api/workflows")
def api_create_workflow(req: WorkflowCreateRequest, current_client: Optional[Dict] = Depends(get_current_client)):
    # Standard clients implicitly own their creations. Admin skips this to explicitly assign.
    cid = current_client["id"] if current_client else req.client_id
    wf = create_workflow(
        name=req.name,
        description=req.description,
        project_id=None,
        client_id=cid,
        steps=req.steps,
        config=req.config,
        variables=req.variables,
        edges=req.edges,
        positions=req.positions,
    )
    return wf


@app.get("/api/workflows/{workflow_id}")
def api_get_workflow(workflow_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    cid = current_client["id"] if current_client else None
    if cid and wf.get("client_id") != cid:
        raise HTTPException(status_code=403, detail="Access denied")
    return wf


@app.put("/api/workflows/{workflow_id}")
def api_update_workflow(workflow_id: str, req: WorkflowUpdateRequest, current_client: Optional[Dict] = Depends(get_current_client)):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    cid = current_client["id"] if current_client else None
    if cid and wf.get("client_id") != cid:
        raise HTTPException(status_code=403, detail="Access denied")
        
    updated = update_workflow(
        workflow_id,
        name=req.name,
        description=req.description,
        steps=req.steps,
        config=req.config,
        variables=req.variables,
        edges=req.edges,
        positions=req.positions,
        client_id=req.client_id,
    )
    return updated


@app.delete("/api/workflows/{workflow_id}")
def api_delete_workflow(workflow_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    cid = current_client["id"] if current_client else None
    if cid and wf.get("client_id") != cid:
        raise HTTPException(status_code=403, detail="Access denied")
        
    success = delete_workflow(workflow_id)
    return {"deleted": success}

# ─── Scheduled Jobs ───
# ─── Scheduled Jobs ───
@app.get("/api/scheduled-jobs")
async def api_list_scheduled_jobs(current_client: Optional[Dict] = Depends(get_current_client)):
    jobs = list_jobs()
    pid = current_client["id"] if current_client else None
    if pid:
        filtered = []
        for j in jobs:
            wf = get_workflow(j["workflow_id"])
            if wf and wf.get("project_id") == pid:
                filtered.append(j)
        return {"jobs": filtered}
    return {"jobs": jobs}

@app.post("/api/scheduled-jobs")
async def api_create_scheduled_job(req: ScheduleJobRequest, current_client: Optional[Dict] = Depends(get_current_client)):
    wf = get_workflow(req.workflow_id)
    if not wf:
        raise HTTPException(404, "Workflow not found")
    pid = current_client["id"] if current_client else None
    if pid and wf.get("project_id") != pid:
        raise HTTPException(403, "Access denied")
    try:
        job_id = add_cron_job(req.workflow_id, req.cron_expr, req.input_prompt)
        return {"job_id": job_id, "status": "success"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/api/scheduled-jobs/{job_id}")
async def api_delete_scheduled_job(job_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    jobs = list_jobs()
    job = next((j for j in jobs if j["id"] == job_id), None)
    if job:
        wf = get_workflow(job["workflow_id"])
        pid = current_client["id"] if current_client else None
        if pid and wf and wf.get("project_id") != pid:
            raise HTTPException(403, "Access denied")
    try:
        remove_job(job_id)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/workflows/{workflow_id}/run")
async def api_run_workflow(workflow_id: str, req: WorkflowRunRequest, current_client: Optional[Dict] = Depends(get_current_client)):
    """Start a workflow execution. Creates or reuses a session.

    Accepts optional input_prompt (injected into first step) and
    input_files (written to session workspace before execution).
    """
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
        
    pid = current_client["id"] if current_client else None
    if pid and wf.get("project_id") != pid:
        raise HTTPException(403, "Access denied")

    if req.session_id:
        session = get_session(req.session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if pid and session.get("project_id") != pid:
            raise HTTPException(status_code=403, detail="Session access denied")
        session_id = req.session_id
    else:
        title = req.session_title or f"Workflow: {wf['name']}"
        session = create_session(project_id=pid, title=title, agent_type="workflow")
        session_id = session["id"]

    # Write input files to workspace before execution
    if req.input_files:
        workspace = get_session_workspace(session_id)
        _write_input_files(workspace, req.input_files)

    run = create_run(workflow_id=workflow_id, session_id=session_id)

    # Resolve variables: merge defaults from workflow definition with run-time overrides
    resolved_vars = _resolve_variables(wf.get("variables", []), req.variables or {})

    await workflow_executor.start_workflow(
        run_id=run["id"],
        workflow=wf,
        session_id=session_id,
        input_prompt=req.input_prompt,
        variables=resolved_vars,
    )

    return {
        "run_id": run["id"],
        "session_id": session_id,
        "status": "running",
        "workflow": wf["name"],
    }


@app.get("/api/workflows/{workflow_id}/runs")
def api_list_workflow_runs(workflow_id: str, limit: int = 50, current_client: Optional[Dict] = Depends(get_current_client)):
    wf = get_workflow(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    pid = current_client["id"] if current_client else None
    if pid and wf.get("project_id") != pid:
        raise HTTPException(403, "Access denied")
    return {"runs": list_runs(workflow_id=workflow_id, limit=limit)}


@app.get("/api/workflow-runs/{run_id}")
def api_get_run(run_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    pid = current_client["id"] if current_client else None
    if pid:
        wf = get_workflow(run["workflow_id"])
        if wf and wf.get("project_id") != pid:
            raise HTTPException(403, "Access denied")
    return run


@app.post("/api/workflow-runs/{run_id}/cancel")
def api_cancel_run(run_id: str, current_client: Optional[Dict] = Depends(get_current_client)):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    pid = current_client["id"] if current_client else None
    if pid:
        wf = get_workflow(run["workflow_id"])
        if wf and wf.get("project_id") != pid:
            raise HTTPException(403, "Access denied")
    success = workflow_executor.cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=400, detail="Run not running or not found")
    return {"cancelled": True}


class SessionWorkflowRunRequest(BaseModel):
    workflow_id: str
    input_prompt: Optional[str] = None
    input_files: Optional[List[WorkflowInputFile]] = None
    variables: Optional[Dict[str, str]] = None  # Variable values overrides


def _write_input_files(workspace: str, files: List[WorkflowInputFile]):
    """Write input files to the session workspace."""
    import base64
    os.makedirs(workspace, exist_ok=True)
    for f in files:
        filepath = os.path.join(workspace, f.filename)
        # Ensure subdirectories exist
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) != '' else workspace, exist_ok=True)
        if f.content_b64:
            with open(filepath, "wb") as fh:
                fh.write(base64.b64decode(f.content_b64))
        elif f.content_text:
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(f.content_text)


def _resolve_variables(
    variable_defs: List[Dict], overrides: Dict[str, str]
) -> Dict[str, str]:
    """Resolve workflow variables by merging defaults with run-time overrides.

    Args:
        variable_defs: [{name, label, type, default, required}] from workflow definition
        overrides: {name: value} provided at run time

    Returns:
        {name: resolved_value} dict ready for substitution
    """
    resolved = {}
    for v in variable_defs:
        name = v.get("name", "")
        if not name:
            continue
        # Use override if provided, otherwise fall back to default
        if name in overrides:
            resolved[name] = str(overrides[name])
        elif "default" in v and v["default"] is not None:
            resolved[name] = str(v["default"])
        elif v.get("required", False):
            # Required variable with no default and no override — use empty string
            resolved[name] = ""
    return resolved


@app.post("/api/sessions/{session_id}/run-workflow")
async def api_run_workflow_in_session(session_id: str, req: SessionWorkflowRunRequest):
    """Run a saved workflow within an existing session.
    
    This allows the Chat/Workspace interface to trigger workflow execution
    using the same session context, workspace, and message history.
    Accepts optional input_prompt, input_files, and variables.
    """
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    wf = get_workflow(req.workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Write input files to workspace before execution
    if req.input_files:
        workspace = get_session_workspace(session_id)
        _write_input_files(workspace, req.input_files)

    run = create_run(workflow_id=req.workflow_id, session_id=session_id)

    # Resolve variables
    resolved_vars = _resolve_variables(wf.get("variables", []), req.variables or {})

    await workflow_executor.start_workflow(
        run_id=run["id"],
        workflow=wf,
        session_id=session_id,
        input_prompt=req.input_prompt,
        variables=resolved_vars,
    )

    return {
        "run_id": run["id"],
        "session_id": session_id,
        "status": "running",
        "workflow": wf["name"],
        "steps": len(wf.get("steps", [])),
    }


# ─── Session Context Sharing Endpoints ────────────────────────────

class ContextLinkRequest(BaseModel):
    target_session_id: str
    link_type: str = 'reference'  # reference | fork | shared_workspace
    label: str = ''
    include_messages: bool = True
    include_files: bool = True
    max_messages: int = 50


@app.post("/api/sessions/{session_id}/context-links")
def api_create_context_link(session_id: str, req: ContextLinkRequest):
    """Link a session to another session for context sharing.

    Link types:
    - reference: Read messages/events from the target session as context
    - fork: This session was forked from the target
    - shared_workspace: Share the workspace directory with the target
    """
    link = create_context_link(
        source_session_id=session_id,
        target_session_id=req.target_session_id,
        link_type=req.link_type,
        label=req.label,
        include_messages=req.include_messages,
        include_files=req.include_files,
        max_messages=req.max_messages,
    )
    if not link:
        raise HTTPException(status_code=404, detail="Session(s) not found")
    return link


@app.get("/api/sessions/{session_id}/context-links")
def api_get_context_links(session_id: str):
    """Get all context links for a session (both incoming and outgoing)."""
    return get_context_links(session_id)


@app.delete("/api/context-links/{link_id}")
def api_delete_context_link(link_id: str):
    """Remove a context link."""
    delete_context_link(link_id)
    return {"deleted": True}


@app.get("/api/sessions/{session_id}/linked-messages")
def api_get_linked_messages(session_id: str, limit: int = 50):
    """Get messages from all linked sessions.

    Returns messages from sessions linked to this one,
    tagged with their source session info.
    """
    return get_linked_messages(session_id, limit_per_link=limit)


class ForkSessionRequest(BaseModel):
    title: str = ''
    agent_type: str = ''
    copy_messages: int = 0  # 0 = reference only, >0 = copy last N messages


@app.post("/api/sessions/{session_id}/fork")
def api_fork_session(session_id: str, req: ForkSessionRequest):
    """Fork a new session from an existing one.

    Creates a new session linked to the source via 'fork' link.
    Optionally copies the last N messages for immediate context.
    """
    result = fork_session(
        source_session_id=session_id,
        title=req.title,
        agent_type=req.agent_type,
        copy_messages=req.copy_messages,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Source session not found")
    return result

from fastapi import UploadFile, File as FastAPIFile


@app.post("/api/upload/{session_id}")
async def api_upload_to_session(session_id: str, file: UploadFile = FastAPIFile(...)):
    """Upload file to session workspace."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    workspace = get_session_workspace(session_id)
    filepath = os.path.join(workspace, file.filename)
    os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else workspace, exist_ok=True)

    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)

    return {
        "filename": file.filename,
        "path": filepath,
        "size": len(content),
        "session_id": session_id,
    }


@app.get("/api/sessions/{session_id}/files")
def api_list_session_files(session_id: str):
    """List files in a session's workspace."""
    session = get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    workspace = get_session_workspace(session_id)
    files = []
    if os.path.isdir(workspace):
        for root, dirs, filenames in os.walk(workspace):
            # Exclude hidden directories like .git
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for entry in filenames:
                if entry.startswith('.'):
                    continue
                full = os.path.join(root, entry)
                if os.path.isfile(full):
                    rel_path = os.path.relpath(full, workspace)
                    files.append({
                        "name": rel_path,
                        "size": os.path.getsize(full),
                        "path": full,
                    })
    return {"files": files, "workspace": workspace}


# ---------------------------------------------
# ─── Environment gate check (shared by REST + WebSocket) ─────
_ENV_GATES = {
    "gemini": "ENABLE_GEMINI_CLI",
    "claude": "ENABLE_CLAUDE_REMOTE_CONTROL",
    "codex": "ENABLE_CODEX_SERVER",
    "ollama": "ENABLE_OLLAMA_API",
    "mflux": "ENABLE_MFLUX_IMAGE",
}

def _check_env_gate(client: str):
    """Raise HTTPException if the agent route is disabled in .env."""
    gate = _ENV_GATES.get(client)
    if gate and os.getenv(gate) != "true":
        raise HTTPException(status_code=403, detail=f"{client} route disabled inside global .env")

@app.post("/execute", response_model=ExecutionResponse)
async def execute_task(req: ExecutionRequest, request: Request):
    """
    Execute via Hand Registry: execute(name, input) → string.
    Direct API calls are auto-recorded into sessions for unified tracking.
    """
    _check_env_gate(req.client)

    # Get available hand with fallback logic
    hand = hand_registry.get_available(req.client, backups=["gemini", "claude", "codex"])
    if not hand:
        raise HTTPException(status_code=404, detail=f"No hand registered for '{req.client}'")

    workspace_str = req.workspace_id or "default_sync"
    workspace_dir = os.path.join(get_workspaces_dir(), workspace_str)

    # ─── Session-aware: record into a session for tracking ─────
    is_ui = "localhost:5173" in request.headers.get("origin", "")
    session_id = None
    if not is_ui:
        # Direct API call — auto-create or reuse an API session
        session = create_session(title=f"API: {req.prompt[:50]}", agent_type=req.client)
        session_id = session["id"]
        add_message(session_id, source="user", content=req.prompt, agent_type=req.client)
        auto_title_session(session_id)
        session_events.emit_event(session_id, EventType.USER_MESSAGE, content=req.prompt, agent="user")
        session_events.emit_event(session_id, EventType.AGENT_SELECTED, agent=req.client, metadata={"source": "direct_api"})

    target_opt_kwargs = {}
    if req.client == "ollama":
        target_opt_kwargs["model"] = req.model or "llama3"

    result = await hand.execute(req.prompt, workspace_dir=workspace_dir, **target_opt_kwargs)

    # Check for rate limit
    from app.hands.base import check_rate_limit
    wait_time = check_rate_limit(result.output)
    if wait_time is not None:
        hand_registry.mark_rate_limited(hand.name, wait_time)
        result.exit_code = 429
        result.output = f"[RATE LIMITED] {hand.name} is paused for {wait_time}s. Original Output:\n{result.output}"

    # ─── Record result into session ─────
    if session_id:
        add_message(session_id, source="agent", content=result.output, agent_type=req.client)
        evt_type = EventType.TOOL_RESULT if result.success else EventType.TOOL_ERROR
        session_events.emit_event(session_id, evt_type, content=result.output[:2000], agent=req.client,
                                  metadata={"exit_code": result.exit_code, "source": "direct_api"})
        session_events.emit_event(session_id, EventType.AGENT_RESPONSE, content=result.output[:2000], agent=req.client)

    return ExecutionResponse(exitCode=result.exit_code, output=result.output)

from fastapi.responses import StreamingResponse
import asyncio
import json

@app.post("/execute/stream")
async def execute_task_stream(req: ExecutionRequest, request: Request):
    """
    Execute streaming LLM task via Hand Registry using ndjson.
    Direct API calls are auto-recorded into sessions for unified tracking.
    """
    _check_env_gate(req.client)

    # Get available hand with fallback
    hand = hand_registry.get_available(req.client, backups=["gemini", "claude", "codex"])
    if not hand:
        raise HTTPException(status_code=404, detail=f"No hand registered for '{req.client}'")

    workspace_str = req.workspace_id or "default_sync"
    workspace_dir = os.path.join(get_workspaces_dir(), workspace_str)

    # ─── Session-aware: record into a session for tracking ─────
    is_ui = "localhost:5173" in request.headers.get("origin", "")
    session_id = None
    if not is_ui:
        session = create_session(title=f"API: {req.prompt[:50]}", agent_type=req.client)
        session_id = session["id"]
        add_message(session_id, source="user", content=req.prompt, agent_type=req.client)
        auto_title_session(session_id)
        session_events.emit_event(session_id, EventType.USER_MESSAGE, content=req.prompt, agent="user")
        session_events.emit_event(session_id, EventType.AGENT_SELECTED, agent=req.client, metadata={"source": "direct_api"})

    q = asyncio.Queue()
    full_output: list = []

    async def stream_log(chunk: str):
        full_output.append(chunk)
        await q.put({"type": "node_execution_log", "log": chunk})

    target_opt_kwargs = {}
    if req.client == "ollama":
        target_opt_kwargs["model"] = req.model or "llama3"

    async def worker():
        try:
            await q.put({"type": "node_execution_started"})
            result = await hand.execute(req.prompt, workspace_dir=workspace_dir, on_log=stream_log, **target_opt_kwargs)
            if result.image_b64:
                await q.put({"type": "node_execution_image", "b64": result.image_b64})
            # Check for rate limit
            from app.hands.base import check_rate_limit
            output_text = "".join(full_output)
            wait_time = check_rate_limit(output_text)
            if wait_time is not None:
                hand_registry.mark_rate_limited(hand.name, wait_time)
                result.exit_code = 429
                rate_str = f"\n\n[RATE LIMITED] {hand.name} is paused for {wait_time}s.\n"
                output_text += rate_str
                await q.put({"type": "node_execution_log", "log": rate_str})

            # Record result into session
            if session_id:
                add_message(session_id, source="agent", content=output_text, agent_type=req.client, image_b64=result.image_b64)
                evt_type = EventType.TOOL_RESULT if result.success else EventType.TOOL_ERROR
                session_events.emit_event(session_id, evt_type, content=output_text[:2000], agent=req.client,
                                          metadata={"exit_code": result.exit_code, "source": "direct_api"})
                session_events.emit_event(session_id, EventType.AGENT_RESPONSE, content=output_text[:2000], agent=req.client)
            await q.put({"type": "node_execution_completed", "exitCode": result.exit_code, "sessionId": session_id})
        except Exception as e:
            await q.put({"type": "node_execution_log", "log": f"\n[Fatal Router Error] {e}\n"})
            await q.put({"type": "node_execution_completed", "exitCode": 1})

    async def generate_ndjson():
        task = asyncio.create_task(worker())
        while True:
            item = await q.get()
            yield json.dumps(item) + "\n"
            if item["type"] == "node_execution_completed":
                break
        await task

    return StreamingResponse(generate_ndjson(), media_type="application/x-ndjson")


# ─── Edge Node Task Callback (CF Worker Queue Consumer) ──────────

class EdgeTaskRequest(BaseModel):
    taskId: str
    hand: str
    prompt: str
    sessionId: Optional[str] = None
    workspaceKey: Optional[str] = None
    callbackUrl: str
    callbackToken: str
    timeoutMs: int = 300000


@app.post("/api/execute")
async def edge_execute_task(req: EdgeTaskRequest, request: Request):
    """
    Called by the CF Worker queue consumer to execute a CLI task on this edge node.
    Runs the hand in the background, then POSTs the result back to callbackUrl.
    """
    # Verify node key
    node_key = os.getenv("NODE_KEY", os.getenv("ADMIN_API_KEY", "sk_admin_route_2025"))
    incoming_key = request.headers.get("X-DCPN-Key", "")
    if incoming_key != node_key:
        raise HTTPException(status_code=401, detail="Invalid node key")

    hand = hand_registry.get_available(req.hand, backups=["gemini", "claude", "codex"])
    if not hand:
        raise HTTPException(status_code=400, detail=f"Hand not available: {req.hand}")

    async def run_and_callback():
        workspace_str = req.workspaceKey or req.sessionId or "edge_task"
        workspace_dir = os.path.join(get_workspaces_dir(), workspace_str)
        os.makedirs(workspace_dir, exist_ok=True)

        try:
            result = await hand.execute(req.prompt, workspace_dir=workspace_dir)
            exit_code = result.exit_code
            output = result.output
        except Exception as e:
            exit_code = 1
            output = str(e)

        # Callback to CF Worker with result
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    req.callbackUrl,
                    json={"exitCode": exit_code, "resultText": output},
                    headers={
                        "Content-Type": "application/json",
                        "X-Callback-Token": req.callbackToken,
                    },
                )
        except Exception as e:
            print(f"[edge-task] Callback failed for task {req.taskId}: {e}")

    asyncio.create_task(run_and_callback())
    return {"accepted": True, "taskId": req.taskId}


# ─── Multi-Agent Execution API (Phase 9) ──────────────────────

class MultiAgentRequest(BaseModel):
    agents: list  # e.g. ["gemini", "claude"]
    prompt: str
    session_id: Optional[str] = None
    workspace_id: Optional[str] = None
    strategy: str = "first_success"  # first_success | majority_vote | best_effort | all
    timeout: float = 300.0

@app.post("/api/multi-agent/run")
async def multi_agent_run(req: MultiAgentRequest):
    """Fan-out a prompt to multiple agents, join results with a strategy."""
    for agent in req.agents:
        _check_env_gate(agent)
        if not hand_registry.get(agent):
            raise HTTPException(404, f"No hand registered for '{agent}'")

    session_id = req.session_id or f"multi_{uuid.uuid4().hex[:12]}"
    workspace_str = req.workspace_id or "default_sync"
    workspace_dir = os.path.join(get_workspaces_dir(), workspace_str)

    result = await orchestrator.multi_agent_run(
        session_id=session_id,
        agents=req.agents,
        prompt=req.prompt,
        workspace_dir=workspace_dir,
        strategy=req.strategy,
        timeout=req.timeout,
    )

    return {
        "session_id": session_id,
        "strategy": req.strategy,
        "agents": req.agents,
        **result,
    }


from fastapi import WebSocket, WebSocketDisconnect

# ---------------------------------------------
# 4a. Background Task Status API
# ---------------------------------------------
@app.get("/api/tasks")
def api_list_tasks():
    """List all running/recent background tasks."""
    return {"tasks": task_manager.get_all_status()}

@app.get("/api/tasks/running")
def api_running_sessions():
    """Get session IDs that have actively running tasks."""
    return {"running_sessions": task_manager.get_running_session_ids()}

@app.get("/api/tasks/{session_id}")
def api_session_tasks(session_id: str):
    """Get all tasks for a session."""
    tasks = task_manager.get_session_tasks(session_id)
    return {"tasks": [t.status.to_dict() for t in tasks]}

@app.get("/api/tasks/history")
def api_task_history(session_id: Optional[str] = None, limit: int = 50):
    """Query persistent task history from SQLite."""
    return {"tasks": get_task_history(session_id=session_id, limit=limit)}

@app.get("/api/analytics")
def api_task_analytics(date: Optional[str] = None, days: int = 7, session_id: Optional[str] = None):
    """Get aggregate task performance analytics."""
    return get_task_analytics(date_str=date, days=days, session_id=session_id)

@app.get("/api/analytics/benchmark")
def api_benchmark(agents: Optional[str] = None, days: int = 30):
    """Compare agent performance head-to-head."""
    agent_list = agents.split(',') if agents else None
    return get_benchmark_comparison(agents=agent_list, days=days)

# ---------------------------------------------
# 4b. Unified API Activity Feed (Middle Desk)
# ---------------------------------------------

@app.get("/api/activity/calls")
def api_activity_calls(
    limit: int = 100,
    category: Optional[str] = None,
    session_id: Optional[str] = None,
    source: Optional[str] = None,
    since_ms: Optional[float] = None,
):
    """Query the API call log with optional filters.
    
    Categories: execution, brain, workflow, session_mutation, session_read,
    context, report, agent, analytics, sandbox, file, websocket, other
    
    Sources: 'api' (external/direct), 'ui' (frontend)
    """
    calls = get_api_calls(
        limit=limit, category=category, session_id=session_id,
        source=source, since_ms=since_ms,
    )
    return {"calls": calls, "total": len(calls)}


@app.get("/api/activity/stats")
def api_activity_stats(hours: int = 24):
    """Aggregate API call statistics over the given time window."""
    return get_api_stats(hours=hours)


@app.get("/api/activity/timeline")
def api_activity_timeline(hours: int = 1, bucket_minutes: int = 5):
    """Time-series API call frequency for live monitoring charts."""
    return {"timeline": get_api_timeline(hours=hours, bucket_minutes=bucket_minutes)}


@app.get("/api/activity/feed")
def api_unified_feed(limit: int = 50, source: Optional[str] = None):
    """Unified activity feed combining API calls, running tasks, and recent sessions.
    
    This is the primary endpoint for the Brain Inspector 'Middle Desk' view,
    merging all activity into a single chronological feed.
    """
    # 1. Recent API execution calls
    execution_calls = get_api_calls(limit=limit, category="execution", source=source)

    # 2. Currently running tasks
    running_tasks = task_manager.get_all_status()

    # 3. Recent sessions (sorted by updated_at desc)
    recent_sessions = list_sessions()[:limit]

    # 4. Recent workflow runs
    from app.workflow_store import list_runs as list_all_runs
    recent_runs = list_all_runs(limit=limit)

    # Build unified feed
    feed = []

    for call in execution_calls:
        feed.append({
            "type": "api_call",
            "timestamp": call.get("created_at", 0),
            "method": call.get("method"),
            "path": call.get("path"),
            "status_code": call.get("status_code"),
            "duration_ms": call.get("duration_ms"),
            "agent": call.get("agent"),
            "session_id": call.get("session_id"),
            "source": call.get("source"),
            "category": call.get("category"),
            "request_id": call.get("request_id"),
        })

    for task in running_tasks:
        feed.append({
            "type": "running_task",
            "timestamp": task.get("started_at", 0),
            "task_id": task.get("task_id"),
            "session_id": task.get("session_id"),
            "agent": task.get("agent"),
            "phase": task.get("phase"),
            "elapsed_ms": task.get("elapsed_ms"),
            "prompt": task.get("prompt", "")[:80],
        })

    for run in recent_runs[:20]:
        feed.append({
            "type": "workflow_run",
            "timestamp": run.get("started_at", 0),
            "run_id": run.get("id"),
            "workflow_id": run.get("workflow_id"),
            "session_id": run.get("session_id"),
            "status": run.get("status"),
        })

    # Sort by timestamp descending
    feed.sort(key=lambda x: x.get("timestamp", 0), reverse=True)

    return {
        "feed": feed[:limit],
        "summary": {
            "total_api_calls": len(execution_calls),
            "running_tasks": len(running_tasks),
            "recent_sessions": len(recent_sessions),
            "recent_workflow_runs": len(recent_runs),
        },
    }


# ---------------------------------------------
# 4c. Session-Aware Native WebSocket Streaming
#     with Background Task Support
# ---------------------------------------------
@app.websocket("/ws/agent")
async def websocket_endpoint(websocket: WebSocket):
    """
    Desktop UI WebSocket handler with background task support.
    - Tasks run independently; switching sessions does NOT stop running tasks
    - Rich status phases: connecting → executing → streaming → finalizing
    - All events are broadcast to the WebSocket regardless of viewed session
    """
    x_api_key = websocket.query_params.get("api_key")
    admin_key = os.getenv("ADMIN_API_KEY", "sk_admin_route_2025")
    
    if not x_api_key:
        await websocket.close(code=1008, reason="API Key is required")
        return
        
    current_client = None
    if x_api_key != admin_key:
        # Accept node tokens (nk_ prefix) — same SSO as CF Worker
        if x_api_key.startswith("nk_"):
            node_token = os.getenv("NODE_TOKEN", "")
            if x_api_key != node_token:
                # Also try validating against CF Worker
                try:
                    import httpx
                    resp = httpx.get(
                        f"{os.getenv('CF_WORKER_URL', '')}/api/projects",
                        headers={"X-API-Key": x_api_key}, timeout=5,
                    )
                    if resp.status_code >= 400:
                        await websocket.close(code=1008, reason="Invalid node token")
                        return
                except Exception:
                    await websocket.close(code=1008, reason="Token validation failed")
                    return
        else:
            current_client = get_client_by_api_key(x_api_key)
            if not current_client:
                await websocket.close(code=1008, reason="Invalid API Key")
                return

    await websocket.accept()
    print("Client natively connected to Python FastAPI WebSocket.")

    # Create a subscriber queue for this connection
    ws_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    task_manager.add_global_subscriber(ws_queue)

    # Background drainer: forwards task events to the WebSocket
    async def drain_queue():
        try:
            while True:
                event = await ws_queue.get()
                try:
                    await websocket.send_json(event)
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    drainer = asyncio.create_task(drain_queue())

    try:
        while True:
            data = await websocket.receive_json()
            
            _session_id = data.get("sessionId")
            if current_client and _session_id:
                sess = get_session(_session_id)
                if sess and sess.get("client_id") and sess.get("client_id") != current_client["id"]:
                    await websocket.send_json({"type": "error", "message": "Access denied for this session"})
                    continue

            # ─── Handle status query ─────
            if data.get("type") == "query_running":
                running = task_manager.get_all_status()
                await websocket.send_json({"type": "running_tasks", "tasks": running})
                continue

            # ─── Handle multi-agent run ─────
            if data.get("type") == "multi_agent_run":
                _agents = data.get("agents", [])
                _prompt = data.get("prompt", "")
                _session_id = data.get("sessionId")
                _strategy = data.get("strategy", "first_success")
                _timeout = data.get("timeout", 300.0)

                if not _agents or not _prompt:
                    await websocket.send_json({"type": "error", "message": "agents and prompt required"})
                    continue

                # Session persistence
                if _session_id:
                    if not get_session(_session_id):
                        try: create_session(title=f"Multi: {_prompt[:40]}", agent_type=_agents[0], session_id=_session_id)
                        except Exception: pass
                    try:
                        add_message(_session_id, source='user', content=f"[Multi-Agent: {', '.join(_agents)}] {_prompt}", agent_type=_agents[0])
                        auto_title_session(_session_id)
                    except Exception: pass

                _workspace_dir = os.path.join(get_workspaces_dir(), _session_id or 'multi_default')

                async def multi_task():
                    async def _ws_send(data):
                        try:
                            await websocket.send_json(data)
                        except Exception:
                            pass  # WS may have closed — non-fatal

                    try:
                        await _ws_send({
                            "type": "multi_agent_started",
                            "sessionId": _session_id,
                            "agents": _agents,
                            "strategy": _strategy,
                        })

                        async def _multi_log(chunk: str):
                            await _ws_send({
                                "type": "node_execution_log",
                                "sessionId": _session_id,
                                "log": chunk,
                            })

                        result = await orchestrator.multi_agent_run(
                            session_id=_session_id or f"multi_{uuid.uuid4().hex[:12]}",
                            agents=_agents,
                            prompt=_prompt,
                            workspace_dir=_workspace_dir,
                            strategy=_strategy,
                            timeout=_timeout,
                            on_log=_multi_log,
                        )

                        # Persist final result
                        if _session_id:
                            try:
                                output_preview = result.get("output", "")[:2000]
                                add_message(_session_id, source='agent', content=output_preview, agent_type="multi")
                            except Exception:
                                pass

                        await _ws_send({
                            "type": "multi_agent_completed",
                            "sessionId": _session_id,
                            "strategy": _strategy,
                            "agents": _agents,
                            "success": result.get("success"),
                            "output": result.get("output", "")[:5000],
                            "selected_agent": result.get("selected_agent"),
                            "all_results": [
                                {
                                    "agent": r.get("agent"),
                                    "success": r.get("success"),
                                    "exit_code": r.get("exit_code"),
                                    "output": r.get("output", "")[:1000],
                                }
                                for r in (result.get("all_results") or [])
                            ],
                        })

                        # Sync workspace after multi-agent
                        if _session_id:
                            asyncio.create_task(sync_session_now(_session_id))

                    except Exception as e:
                        print(f"[multi-agent] Error: {e}")
                        await _ws_send({
                            "type": "multi_agent_error",
                            "sessionId": _session_id,
                            "error": str(e),
                        })

                asyncio.create_task(multi_task())
                continue

            # ─── Parse execution request ─────
            session_id = data.get("sessionId")
            tracking_task_id = data.get("trackingTaskId")

            if "type" in data and data["type"] == "command":
                mode = data.get("mode")
                prompt = data.get("content")
                node_id = data.get("nodeId", "sync_chat")
                workspace_str = data.get("workspaceId", "default_bridge")
                target_model = data.get("model")
            elif "type" in data and data["type"] == "execute_node":
                mode = data.get("client")
                prompt = data.get("prompt")
                node_id = data.get("nodeId", "execute_node")
                workspace_str = data.get("workflowId", "fallback_node")
                target_model = data.get("model")
            else:
                mode = data.get("mode") or data.get("client")
                prompt = data.get("content") or data.get("prompt")
                node_id = "generic"
                workspace_str = "default_bridge"
                target_model = data.get("model")

            if not mode or not prompt:
                continue

            # ─── Session persistence: store user message ─────
            if session_id:
                # Ensure session exists locally (may have been created on CF Worker)
                if not get_session(session_id):
                    try:
                        create_session(title=f"Remote: {prompt[:40]}", agent_type=mode, session_id=session_id)
                    except Exception:
                        pass
                try:
                    add_message(session_id, source='user', content=prompt, agent_type=mode)
                    auto_title_session(session_id)
                except Exception:
                    pass
                try:
                    session_events.emit_event(
                        session_id, EventType.USER_MESSAGE,
                        content=prompt, agent="user",
                    )
                except Exception:
                    pass

            # ─── Environment gate check ─────
            gate = _ENV_GATES.get(mode)
            if gate and os.getenv(gate) != "true":
                await websocket.send_json({"type": "node_execution_log", "nodeId": node_id, "sessionId": session_id, "log": f"❌ {mode} disabled locally in .env\n"})
                if session_id:
                    session_events.emit_event(session_id, EventType.ERROR, content=f"{mode} disabled in .env", agent=mode)
                continue

            # ─── Resolve hand from registry ─────
            hand = hand_registry.get(mode)
            if not hand:
                await websocket.send_json({"type": "node_execution_log", "nodeId": node_id, "sessionId": session_id, "log": f"❌ No hand registered for '{mode}'\n"})
                if session_id:
                    session_events.emit_event(session_id, EventType.ERROR, content=f"No hand registered: {mode}", agent=mode)
                continue

            # ─── Create background task and launch ─────
            bg_task = task_manager.create_task(
                session_id=session_id or "untracked",
                agent=mode,
                prompt=prompt,
            )
            task_id = bg_task.task_id

            # Emit startup with task_id
            await websocket.send_json({
                "type": "node_execution_started",
                "nodeId": node_id,
                "sessionId": session_id,
                "taskId": task_id,
            })

            if session_id:
                session_events.emit_event(
                    session_id, EventType.AGENT_SELECTED,
                    agent=mode,
                    metadata={"hand_type": hand.hand_type, "node_id": node_id, "task_id": task_id},
                )

            # ─── Background worker function ─────
            async def run_task(
                _task_id: str, _session_id: str, _mode: str, _prompt: str,
                _hand, _node_id: str, _workspace_str: str, _target_model: str = None,
                _tracking_task_id: str = None,
            ):
                full_out_array = []

                async def stream_log(chunk: str):
                    full_out_array.append(chunk)
                    await task_manager.emit_output(_task_id, chunk, source="agent")

                try:
                    # Phase: CONNECTING
                    await task_manager.update_phase(_task_id, TaskPhase.CONNECTING)

                    # Resolve workspace
                    if _session_id and _session_id != "untracked":
                        workspace_dir = get_session_workspace(_session_id)
                    else:
                        workspace_dir = os.path.join(get_workspaces_dir(), _workspace_str)
                        os.makedirs(workspace_dir, exist_ok=True)

                    target_opt_kwargs = {}
                    if _mode == "ollama" and _target_model:
                        target_opt_kwargs["model"] = _target_model

                    if _session_id and _session_id != "untracked":
                        session_events.emit_event(
                            _session_id, EventType.TOOL_CALL,
                            content=_prompt, agent=_mode,
                            metadata={"hand_type": _hand.hand_type, "workspace": workspace_dir, "task_id": _task_id},
                        )

                    # Phase: EXECUTING
                    await task_manager.update_phase(_task_id, TaskPhase.EXECUTING)

                    # Phase: STREAMING (set once first output arrives)
                    first_chunk = True
                    original_stream_log = stream_log

                    async def stream_log_with_phase(chunk: str):
                        nonlocal first_chunk
                        if first_chunk:
                            await task_manager.update_phase(_task_id, TaskPhase.STREAMING)
                            first_chunk = False
                        await original_stream_log(chunk)

                    # Execute via Hand Protocol
                    print(f"[Hand:{_hand.name}] Background task {_task_id} (session={_session_id}).")
                    result = await _hand.execute(
                        _prompt, workspace_dir=workspace_dir,
                        on_log=stream_log_with_phase, **target_opt_kwargs
                    )

                    # Phase: FINALIZING
                    await task_manager.update_phase(_task_id, TaskPhase.FINALIZING)

                    # Emit tool result/error event
                    if _session_id and _session_id != "untracked":
                        if result.success:
                            session_events.emit_event(
                                _session_id, EventType.TOOL_RESULT,
                                content=result.output[:2000],
                                agent=_mode,
                                metadata={"exit_code": result.exit_code, "output_length": len(result.output), "task_id": _task_id},
                            )
                        else:
                            session_events.emit_event(
                                _session_id, EventType.TOOL_ERROR,
                                content=result.output[:2000],
                                agent=_mode,
                                metadata={"exit_code": result.exit_code, "task_id": _task_id},
                            )

                        session_events.emit_event(
                            _session_id, EventType.METRIC,
                            agent=_mode,
                            metadata={
                                "input_tokens": len(_prompt) // 4,
                                "output_tokens": len(result.output) // 4,
                                "task_id": _task_id,
                            },
                        )

                    # Handle image output
                    if result.image_b64:
                        full_out_array.append("\n[System] Graphic successfully generated.")
                        await task_manager.emit_event(_task_id, {
                            "type": "node_execution_image",
                            "nodeId": _node_id,
                            "b64": result.image_b64,
                        })

                    # Session persistence
                    agent_output = "".join(full_out_array)
                    if _session_id and _session_id != "untracked":
                        add_message(
                            _session_id, source='agent', content=agent_output,
                            agent_type=_mode, image_b64=result.image_b64,
                        )
                        session_events.emit_event(
                            _session_id, EventType.AGENT_RESPONSE,
                            content=agent_output[:2000], agent=_mode,
                            metadata={"has_image": bool(result.image_b64), "task_id": _task_id},
                        )

                    # Phase: COMPLETED
                    await task_manager.update_phase(_task_id, TaskPhase.COMPLETED, exit_code=result.exit_code)

                    # Emit completed event
                    await task_manager.emit_event(_task_id, {
                        "type": "node_execution_completed",
                        "nodeId": _node_id,
                        "sessionId": _session_id,
                        "agent": _mode,
                        "output": result.output,
                        "exitCode": result.exit_code,
                        "trackingTaskId": _tracking_task_id,
                    })

                    # Sync workspace files to R2 after execution
                    asyncio.create_task(sync_session_now(_session_id or ""))

                except Exception as e:
                    print(f"[BackgroundTask:{_task_id}] Error: {e}")
                    await task_manager.update_phase(_task_id, TaskPhase.FAILED, exit_code=1, error=str(e))
                    await task_manager.emit_event(_task_id, {
                        "type": "node_execution_completed",
                        "nodeId": _node_id,
                        "exitCode": 1,
                    })

            # Launch as background task — does NOT block the WS loop
            _asyncio_task = asyncio.create_task(run_task(
                task_id, session_id, mode, prompt,
                hand, node_id, workspace_str,
                target_model if 'target_model' in dir() else None,
                tracking_task_id,
            ))
            bg_task.asyncio_task = _asyncio_task

    except WebSocketDisconnect:
        print("Frontend UI disconnected normally.")
    except Exception as e:
        print(f"WebSocket execution crash globally: {e}")
    finally:
        drainer.cancel()
        task_manager.remove_global_subscriber(ws_queue)
        # NOTE: Running background tasks continue even after WS disconnect
