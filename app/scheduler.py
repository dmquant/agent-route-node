import asyncio
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from typing import Dict, Any, Optional

from app.workflow_store import get_workflow, create_run
from app.session_store import create_session, get_session_workspace
from app.workflow_executor import workflow_executor
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

from app.config import get_db_path
# Find the absolute path for the sqlite DB based on the session store's logic
DB_PATH = os.getenv("SQLITE_DB_PATH", get_db_path())
jobstores = {
    'default': SQLAlchemyJobStore(url=f'sqlite:///{DB_PATH}')
}

scheduler = AsyncIOScheduler(jobstores=jobstores)

async def run_scheduled_workflow(workflow_id: str, input_prompt: Optional[str] = None, variables: Optional[Dict[str, Any]] = None):
    """
    Automated job triggered by APScheduler.
    Executes a workflow in a brand new session.
    """
    wf = get_workflow(workflow_id)
    if not wf:
        logger.error(f"Scheduled job failed: Workflow {workflow_id} not found")
        return

    # Create an isolated session for this automated run
    title = f"Scheduled Run: {wf['name']}"
    session = create_session(title=title, agent_type="workflow")
    session_id = session["id"]

    run = create_run(workflow_id=workflow_id, session_id=session_id)
    
    # Very basic variable resolution matching what main.py does
    resolved_vars = {}
    if variables:
        resolved_vars.update(variables)

    logger.info(f"Triggering scheduled workflow {workflow_id} in session {session_id}")

    # Kickoff the workflow execution
    await workflow_executor.start_workflow(
        run_id=run["id"],
        workflow=wf,
        session_id=session_id,
        input_prompt=input_prompt,
        variables=resolved_vars,
    )

def start_scheduler():
    """Start the APScheduler background thread."""
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler initialized and started")

def add_cron_job(workflow_id: str, cron_expr: str, input_prompt: Optional[str] = None):
    """
    cron_expr: e.g. "*/5 * * * *" for every 5 minutes
    Uses the croniter-compatible parsing natively in APScheduler.
    """
    from apscheduler.triggers.cron import CronTrigger
    
    # basic conversion from traditional cron string to CronTrigger
    # apscheduler prefers breakdown by parts, but we can parse cron format
    # "minute hour day month day_of_week"
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        raise ValueError("cron_expr must be a 5-part cron string, e.g. '* * * * *'")
    
    trigger = CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4]
    )
    
    job = scheduler.add_job(
        run_scheduled_workflow,
        trigger=trigger,
        kwargs={
            "workflow_id": workflow_id,
            "input_prompt": input_prompt,
        },
        replace_existing=True,
        # Unique ID combining workflow and prompt
        id=f"wf_{workflow_id}_{abs(hash(input_prompt))}"
    )
    logger.info(f"Added scheduled job {job.id} for workflow {workflow_id}")
    return job.id

def list_jobs():
    jobs = scheduler.get_jobs()
    return [
        {
            "id": j.id,
            "workflow_id": j.kwargs.get("workflow_id"),
            "input_prompt": j.kwargs.get("input_prompt"),
            "next_run_time": j.next_run_time.isoformat() if j.next_run_time else None,
        } for j in jobs
    ]

def remove_job(job_id: str):
    scheduler.remove_job(job_id)
    logger.info(f"Removed scheduled job {job_id}")
