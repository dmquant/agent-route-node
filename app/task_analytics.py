"""
Task Analytics Engine — aggregate metrics from task_log and session events.

Provides:
- Overall success rate, average latency, throughput
- Per-agent performance breakdown
- Hourly heatmap data
- Error analysis
- Benchmark comparison between agents
"""

import sqlite3
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

from app.config import get_db_path
DB_PATH = get_db_path()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ts_range(date_str: Optional[str] = None, days: int = 1):
    """Return (start_epoch_s, end_epoch_s) backwards from the given date or today."""
    if date_str:
        day = datetime.strptime(date_str, "%Y-%m-%d")
    else:
        day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
    if days >= 1:
        start = (day - timedelta(days=days - 1)).timestamp()
    else:
        start = day.timestamp()
        
    end = (day + timedelta(days=1)).timestamp()
    return start, end


def get_task_analytics(
    date_str: Optional[str] = None,
    days: int = 7,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compute aggregate task analytics over a time range.
    Returns metrics, per-agent breakdown, hourly heatmap, error analysis.
    """
    start_s, end_s = _ts_range(date_str, days)
    # task_log.started_at is in milliseconds
    start_ms = start_s * 1000
    end_ms = end_s * 1000

    conn = _get_conn()

    # Build query with optional session filter
    where = "WHERE started_at >= ? AND started_at < ?"
    params: list = [start_ms, end_ms]
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)

    rows = conn.execute(
        f"SELECT * FROM task_log {where} ORDER BY started_at DESC", params
    ).fetchall()

    if not rows:
        conn.close()
        return _empty_analytics(date_str, days)

    tasks = [dict(r) for r in rows]
    total = len(tasks)

    # ─── Core Metrics ─────────────────────
    completed = [t for t in tasks if t["phase"] == "completed"]
    failed = [t for t in tasks if t["phase"] == "failed"]
    success_count = len(completed)
    error_count = len(failed)
    success_rate = (success_count / total * 100) if total > 0 else 0

    latencies = [t["elapsed_ms"] for t in completed if t["elapsed_ms"] and t["elapsed_ms"] > 0]
    avg_latency = sum(latencies) / len(latencies) if latencies else 0
    p50_latency = sorted(latencies)[len(latencies) // 2] if latencies else 0
    p95_latency = sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 0
    max_latency = max(latencies) if latencies else 0
    min_latency = min(latencies) if latencies else 0

    total_output_bytes = sum(t.get("output_bytes", 0) or 0 for t in tasks)
    total_output_chunks = sum(t.get("output_chunks", 0) or 0 for t in tasks)

    # ─── Per-Agent Breakdown ─────────────────────
    agent_stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "total": 0, "success": 0, "failed": 0,
        "latencies": [], "output_bytes": 0, "output_chunks": 0,
        "errors": [],
    })

    for t in tasks:
        agent = t["agent"]
        agent_stats[agent]["total"] += 1
        agent_stats[agent]["output_bytes"] += t.get("output_bytes", 0) or 0
        agent_stats[agent]["output_chunks"] += t.get("output_chunks", 0) or 0

        if t["phase"] == "completed":
            agent_stats[agent]["success"] += 1
            if t["elapsed_ms"] and t["elapsed_ms"] > 0:
                agent_stats[agent]["latencies"].append(t["elapsed_ms"])
        elif t["phase"] == "failed":
            agent_stats[agent]["failed"] += 1
            if t.get("error"):
                agent_stats[agent]["errors"].append({
                    "task_id": t["task_id"],
                    "error": t["error"][:200],
                    "timestamp": t["started_at"],
                })

    agent_breakdown = {}
    for agent, s in agent_stats.items():
        lats = s["latencies"]
        agent_breakdown[agent] = {
            "total": s["total"],
            "success": s["success"],
            "failed": s["failed"],
            "success_rate": (s["success"] / s["total"] * 100) if s["total"] > 0 else 0,
            "avg_latency_ms": round(sum(lats) / len(lats)) if lats else 0,
            "p50_latency_ms": round(sorted(lats)[len(lats) // 2]) if lats else 0,
            "p95_latency_ms": round(sorted(lats)[int(len(lats) * 0.95)]) if lats else 0,
            "max_latency_ms": round(max(lats)) if lats else 0,
            "output_bytes": s["output_bytes"],
            "output_chunks": s["output_chunks"],
            "recent_errors": s["errors"][:5],
        }

    # ─── Hourly Heatmap ─────────────────────
    hourly: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "success": 0, "failed": 0})
    for t in tasks:
        try:
            hour = datetime.fromtimestamp(t["started_at"] / 1000).strftime("%H:00")
            hourly[hour]["total"] += 1
            if t["phase"] == "completed":
                hourly[hour]["success"] += 1
            elif t["phase"] == "failed":
                hourly[hour]["failed"] += 1
        except (ValueError, OSError):
            pass

    hourly_data = []
    for h in range(24):
        key = f"{h:02d}:00"
        entry = hourly.get(key, {"total": 0, "success": 0, "failed": 0})
        hourly_data.append({"hour": key, **entry})

    # ─── Daily Breakdown (for multi-day ranges) ─────────────────────
    daily: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "success": 0, "failed": 0})
    for t in tasks:
        try:
            day = datetime.fromtimestamp(t["started_at"] / 1000).strftime("%Y-%m-%d")
            daily[day]["total"] += 1
            if t["phase"] == "completed":
                daily[day]["success"] += 1
            elif t["phase"] == "failed":
                daily[day]["failed"] += 1
        except (ValueError, OSError):
            pass

    daily_data = sorted(
        [{"date": d, **v} for d, v in daily.items()],
        key=lambda x: x["date"],
    )

    # ─── Recent Errors ─────────────────────
    recent_errors = []
    for t in failed[:10]:
        recent_errors.append({
            "task_id": t["task_id"],
            "session_id": t["session_id"],
            "agent": t["agent"],
            "error": (t.get("error") or "Unknown")[:300],
            "prompt": (t.get("prompt") or "")[:100],
            "elapsed_ms": t.get("elapsed_ms", 0),
            "timestamp": t["started_at"],
        })

    # ─── Top Sessions by task count ─────────────────────
    session_tasks: Dict[str, int] = defaultdict(int)
    for t in tasks:
        session_tasks[t["session_id"]] += 1
    top_sessions = sorted(
        [{"session_id": sid, "task_count": cnt} for sid, cnt in session_tasks.items()],
        key=lambda x: x["task_count"],
        reverse=True,
    )[:10]

    conn.close()

    return {
        "date": date_str or datetime.now().strftime("%Y-%m-%d"),
        "days": days,
        "metrics": {
            "total_tasks": total,
            "success_count": success_count,
            "error_count": error_count,
            "success_rate": round(success_rate, 1),
            "avg_latency_ms": round(avg_latency),
            "p50_latency_ms": round(p50_latency),
            "p95_latency_ms": round(p95_latency),
            "max_latency_ms": round(max_latency),
            "min_latency_ms": round(min_latency),
            "total_output_bytes": total_output_bytes,
            "total_output_chunks": total_output_chunks,
            "unique_agents": len(agent_breakdown),
            "unique_sessions": len(session_tasks),
        },
        "agent_breakdown": agent_breakdown,
        "hourly_heatmap": hourly_data,
        "daily_breakdown": daily_data,
        "recent_errors": recent_errors,
        "top_sessions": top_sessions,
    }


def get_benchmark_comparison(
    agents: Optional[List[str]] = None,
    days: int = 30,
) -> Dict[str, Any]:
    """
    Compare agent performance for benchmarking.
    Returns head-to-head comparison across agents.
    """
    analytics = get_task_analytics(days=days)
    breakdown = analytics["agent_breakdown"]

    if agents:
        breakdown = {k: v for k, v in breakdown.items() if k in agents}

    if not breakdown:
        return {"agents": [], "comparison": {}, "winner": None}

    # Determine winners by category
    categories = {}
    
    # Fastest (lowest avg latency)
    agents_with_latency = {k: v for k, v in breakdown.items() if v["avg_latency_ms"] > 0}
    if agents_with_latency:
        categories["fastest"] = min(agents_with_latency, key=lambda k: agents_with_latency[k]["avg_latency_ms"])

    # Most reliable (highest success rate)
    agents_with_tasks = {k: v for k, v in breakdown.items() if v["total"] > 0}
    if agents_with_tasks:
        categories["most_reliable"] = max(agents_with_tasks, key=lambda k: agents_with_tasks[k]["success_rate"])

    # Most used
    if agents_with_tasks:
        categories["most_used"] = max(agents_with_tasks, key=lambda k: agents_with_tasks[k]["total"])

    # Most productive (highest output bytes)
    if agents_with_tasks:
        categories["most_productive"] = max(agents_with_tasks, key=lambda k: agents_with_tasks[k]["output_bytes"])

    # Overall winner by scoring
    scores: Dict[str, float] = defaultdict(float)
    for agent, data in breakdown.items():
        # Weighted score: 40% reliability, 30% speed, 30% throughput
        if data["total"] > 0:
            reliability_score = data["success_rate"]
            speed_score = max(0, 100 - (data["avg_latency_ms"] / 1000))  # Penalize slow
            throughput_score = min(100, data["output_bytes"] / max(1, data["total"]) / 100)
            scores[agent] = reliability_score * 0.4 + speed_score * 0.3 + throughput_score * 0.3

    overall_winner = max(scores, key=lambda k: scores[k]) if scores else None

    return {
        "agents": list(breakdown.keys()),
        "comparison": breakdown,
        "categories": categories,
        "scores": {k: round(v, 1) for k, v in scores.items()},
        "winner": overall_winner,
        "period_days": days,
    }


def _empty_analytics(date_str, days):
    return {
        "date": date_str or datetime.now().strftime("%Y-%m-%d"),
        "days": days,
        "metrics": {
            "total_tasks": 0, "success_count": 0, "error_count": 0,
            "success_rate": 0, "avg_latency_ms": 0, "p50_latency_ms": 0,
            "p95_latency_ms": 0, "max_latency_ms": 0, "min_latency_ms": 0,
            "total_output_bytes": 0, "total_output_chunks": 0,
            "unique_agents": 0, "unique_sessions": 0,
        },
        "agent_breakdown": {},
        "hourly_heatmap": [{"hour": f"{h:02d}:00", "total": 0, "success": 0, "failed": 0} for h in range(24)],
        "daily_breakdown": [],
        "recent_errors": [],
        "top_sessions": [],
    }
