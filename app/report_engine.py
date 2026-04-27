"""
Daily Usage Report Engine
Aggregates statistics from sessions.db and logs.db for daily/weekly reports.
Can invoke an AI agent to produce a narrative summary.
"""
import sqlite3
import os
import time
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

from app.config import get_db_path, get_data_dir
SESSION_DB = get_db_path()
LOGS_DB = os.path.join(get_data_dir(), 'logs.db')


def _ts_range(date_str: Optional[str] = None, days: int = 1):
    """Return (start_ms, end_ms) for a date string like '2026-04-09' or today."""
    if date_str:
        day = datetime.strptime(date_str, '%Y-%m-%d')
    else:
        day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = int(day.timestamp() * 1000)
    end = int((day + timedelta(days=days)).timestamp() * 1000)
    return start, end


def _safe_connect(db_path: str):
    """Safely connect to a SQLite DB."""
    if not os.path.isfile(db_path):
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def get_daily_stats(date_str: Optional[str] = None, days: int = 1) -> Dict[str, Any]:
    """
    Aggregate statistics for a date range.
    Returns {sessions, messages, agents, errors, timeline, top_sessions, ...}
    """
    start_ms, end_ms = _ts_range(date_str, days)
    result = {
        "date": date_str or datetime.now().strftime('%Y-%m-%d'),
        "days": days,
        "period_start": start_ms,
        "period_end": end_ms,
    }

    # ─── Session & Message Stats ─────────────────────
    conn = _safe_connect(SESSION_DB)
    if not conn:
        result.update({
            "total_sessions": 0, "total_messages": 0,
            "user_queries": 0, "agent_responses": 0,
            "estimated_input_tokens": 0, "estimated_output_tokens": 0,
            "agent_breakdown": {}, "hourly_activity": [],
            "top_sessions": [], "session_details": [],
            "errors": [],
        })
        return result

    # Messages in range are the source of truth for daily activity. Do not
    # pre-filter sessions by updated_at alone: a session updated today can
    # otherwise leak into every earlier report window after its created_at.
    messages = conn.execute(
        'SELECT * FROM messages WHERE created_at >= ? AND created_at < ? ORDER BY created_at ASC',
        (start_ms, end_ms)
    ).fetchall()
    session_ids = sorted({m['session_id'] for m in messages if m['session_id']})
    if session_ids:
        placeholders = ','.join('?' * len(session_ids))
        sessions = conn.execute(
            f'SELECT * FROM sessions WHERE id IN ({placeholders}) ORDER BY updated_at DESC',
            tuple(session_ids)
        ).fetchall()
    else:
        sessions = []

    # Aggregate by agent
    agent_breakdown = defaultdict(lambda: {"queries": 0, "responses": 0, "input_tokens": 0, "output_tokens": 0, "sessions": set()})
    hourly = defaultdict(lambda: {"queries": 0, "responses": 0})
    user_queries = 0
    agent_responses = 0
    total_input_tokens = 0
    total_output_tokens = 0
    errors = []

    for m in messages:
        hour = datetime.fromtimestamp(m['created_at'] / 1000).strftime('%H:00')
        agent = m['agent_type'] or 'unknown'
        content = m['content'] or ''

        if m['source'] == 'user':
            user_queries += 1
            tokens = _estimate_tokens(content)
            total_input_tokens += tokens
            agent_breakdown[agent]["queries"] += 1
            agent_breakdown[agent]["input_tokens"] += tokens
            hourly[hour]["queries"] += 1
        else:  # assistant/agent
            agent_responses += 1
            tokens = _estimate_tokens(content)
            total_output_tokens += tokens
            agent_breakdown[agent]["responses"] += 1
            agent_breakdown[agent]["output_tokens"] += tokens
            hourly[hour]["responses"] += 1

            # Detect errors
            if any(kw in content.lower() for kw in ['error', 'failed', 'exception', 'traceback', 'permission denied']):
                # Extract first error-like line
                for line in content.split('\n'):
                    if any(kw in line.lower() for kw in ['error', 'failed', 'exception', 'traceback']):
                        errors.append({
                            "session_id": m['session_id'],
                            "agent": agent,
                            "message": line.strip()[:200],
                            "timestamp": m['created_at'],
                        })
                        break

        agent_breakdown[agent]["sessions"].add(m['session_id'])

    # Serialize agent breakdown
    agent_stats = {}
    for agent, stats in agent_breakdown.items():
        agent_stats[agent] = {
            "queries": stats["queries"],
            "responses": stats["responses"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "total_tokens": stats["input_tokens"] + stats["output_tokens"],
            "session_count": len(stats["sessions"]),
        }

    # Hourly timeline (sorted)
    hourly_list = []
    for h in range(24):
        key = f"{h:02d}:00"
        entry = hourly.get(key, {"queries": 0, "responses": 0})
        hourly_list.append({
            "hour": key,
            "queries": entry["queries"],
            "responses": entry["responses"],
            "total": entry["queries"] + entry["responses"],
        })

    # Top sessions by message count
    session_details = []
    for s in sessions:
        sid = s['id']
        msg_count = conn.execute('SELECT COUNT(*) as c FROM messages WHERE session_id=? AND created_at >= ? AND created_at < ?', (sid, start_ms, end_ms)).fetchone()['c']
        if msg_count > 0:
            first_msg = conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND source='user' AND created_at >= ? AND created_at < ? ORDER BY created_at ASC LIMIT 1",
                (sid, start_ms, end_ms)
            ).fetchone()
            session_details.append({
                "id": sid,
                "title": s['title'],
                "agent_type": s['agent_type'],
                "message_count": msg_count,
                "created_at": s['created_at'],
                "updated_at": s['updated_at'],
                "first_query": (first_msg['content'][:100] + '...') if first_msg and len(first_msg['content']) > 100 else (first_msg['content'] if first_msg else ''),
            })
    session_details.sort(key=lambda x: x['message_count'], reverse=True)

    conn.close()

    # ─── Historical Logs Stats ─────────────────────
    log_stats = {"total": 0, "success": 0, "error": 0, "running": 0}
    log_entries = []
    logs_conn = _safe_connect(LOGS_DB)
    if logs_conn:
        logs = logs_conn.execute(
            'SELECT * FROM historical_logs WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp DESC',
            (start_ms, end_ms)
        ).fetchall()
        for log in logs:
            log_stats["total"] += 1
            status = (log['status'] or '').lower()
            if status == 'success':
                log_stats["success"] += 1
            elif status == 'error':
                log_stats["error"] += 1
            else:
                log_stats["running"] += 1
            log_entries.append({
                "id": log['id'],
                "title": log['title'],
                "agent": log['agent'],
                "status": log['status'],
                "timestamp": log['timestamp'],
            })
        logs_conn.close()

    result.update({
        "total_sessions": len(sessions),
        "active_sessions": len([s for s in session_details if s['message_count'] > 0]),
        "total_messages": len(messages),
        "user_queries": user_queries,
        "agent_responses": agent_responses,
        "estimated_input_tokens": total_input_tokens,
        "estimated_output_tokens": total_output_tokens,
        "estimated_total_tokens": total_input_tokens + total_output_tokens,
        "agent_breakdown": agent_stats,
        "hourly_activity": hourly_list,
        "top_sessions": session_details[:10],
        "session_details": session_details,
        "errors": errors[:20],
        "error_count": len(errors),
        "log_stats": log_stats,
        "log_entries": log_entries[:20],
    })

    return result


def build_report_prompt(stats: Dict[str, Any]) -> str:
    """Build a structured prompt for AI report generation from stats."""
    date = stats.get("date", "today")
    total_sessions = stats.get("total_sessions", 0)
    total_messages = stats.get("total_messages", 0)
    user_queries = stats.get("user_queries", 0)
    agent_responses = stats.get("agent_responses", 0)
    total_tokens = stats.get("estimated_total_tokens", 0)
    errors = stats.get("errors", [])
    agent_breakdown = stats.get("agent_breakdown", {})
    top_sessions = stats.get("top_sessions", [])
    log_stats = stats.get("log_stats", {})

    prompt = f"""Generate a comprehensive, professional Daily Usage Report for the AI Agent Workspace for {date}.

## Raw Statistics
- **Sessions**: {total_sessions} total, {stats.get('active_sessions', 0)} active
- **Messages**: {total_messages} total ({user_queries} user queries, {agent_responses} agent responses)
- **Estimated Tokens**: ~{total_tokens:,} total ({stats.get('estimated_input_tokens', 0):,} input, {stats.get('estimated_output_tokens', 0):,} output)
- **Execution Logs**: {log_stats.get('total', 0)} total ({log_stats.get('success', 0)} success, {log_stats.get('error', 0)} errors)

## Agent Breakdown
"""
    for agent, data in agent_breakdown.items():
        prompt += f"- **{agent}**: {data['queries']} queries, {data['responses']} responses, ~{data['total_tokens']:,} tokens across {data['session_count']} sessions\n"

    prompt += "\n## Top Sessions (by activity)\n"
    for s in top_sessions[:5]:
        prompt += f"- [{s['agent_type']}] \"{s['title']}\" — {s['message_count']} messages\n"
        if s.get('first_query'):
            prompt += f"  First query: \"{s['first_query']}\"\n"

    if errors:
        prompt += f"\n## Errors & Issues ({len(errors)} detected)\n"
        for e in errors[:10]:
            prompt += f"- [{e['agent']}] {e['message']}\n"

    prompt += """
## Required Report Structure
Please generate a report with these sections:

### 1. Executive Summary
A 2-3 sentence overview of the day's activity, highlighting key accomplishments.

### 2. Activity Summary
Break down the day's work by agent type. What was each agent used for? What was accomplished?

### 3. Key Accomplishments
List the main things that were achieved today based on session titles and queries.

### 4. Issues & Errors
Analyze any errors encountered. What went wrong? What are the root causes?

### 5. Recommendations
Based on the usage patterns, suggest:
- Optimization opportunities (e.g., underused agents, overloaded sessions)
- Error prevention strategies
- Workflow improvements

### 6. Token Usage Analysis
Comment on token efficiency. Which agents consumed the most? Is this expected?

Format the report in clean Markdown with headers, bullet points, and emphasis where appropriate.
"""
    return prompt
