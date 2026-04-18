"""
Workflow Executor — runs workflow steps through the unified task_manager pipeline.

Supports two execution modes:
  1. **DAG mode** (when edges are present): Topological sort determines execution
     order. Each step receives inputs resolved from parent edges, enabling
     non-linear, branching, and parallel-ready execution.
  2. **Linear mode** (fallback, no edges): Steps execute sequentially by index,
     with simple prev_output chaining. Preserves backward compatibility.

Each step is executed as a proper BackgroundTask, with full message logging
and session event emission. This means:
- Brain Inspector works for workflow-triggered sessions
- Messages appear in session history (user prompt + agent response)
- Running tasks show in Dashboard / task list
- Session workspace is shared across all workflow steps
"""

import os
import re
import json
import time
import asyncio
from collections import defaultdict, deque
from typing import Dict, List, Any, Optional, Callable, Tuple

from app.hands.registry import hand_registry
from app.workflow_store import update_run, get_run
from app.session_store import (
    create_session, get_session, get_session_workspace,
    add_message, auto_title_session,
)
from app.session.manager import session_events
from app.session.events import EventType
from app.tasks import task_manager, TaskPhase

# Default timeout per step: 1 hour (agents can take a while for complex tasks)
DEFAULT_STEP_TIMEOUT = 3600


# ─── DAG Utilities ──────────────────────────────────────

def topological_sort(step_ids: List[str], edges: List[Dict]) -> List[str]:
    """Return step IDs in topological execution order (Kahn's algorithm).

    Args:
        step_ids: All step IDs to sort.
        edges: List of edge dicts with 'source' and 'target' keys.

    Returns:
        List of step IDs in dependency-respecting order.

    Raises:
        ValueError: If the graph contains a cycle.
    """
    # Build adjacency list and in-degree map
    id_set = set(step_ids)
    adjacency: Dict[str, List[str]] = defaultdict(list)
    in_degree: Dict[str, int] = {sid: 0 for sid in step_ids}

    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src in id_set and tgt in id_set:
            adjacency[src].append(tgt)
            in_degree[tgt] = in_degree.get(tgt, 0) + 1

    # Enqueue all nodes with no incoming edges
    queue = deque(sid for sid in step_ids if in_degree[sid] == 0)
    sorted_ids: List[str] = []

    while queue:
        node = queue.popleft()
        sorted_ids.append(node)
        for neighbor in adjacency[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(sorted_ids) != len(step_ids):
        # Cycle detected — report which nodes are involved
        remaining = set(step_ids) - set(sorted_ids)
        raise ValueError(
            f"DAG contains a cycle involving nodes: {remaining}. "
            f"Sorted {len(sorted_ids)} of {len(step_ids)} nodes."
        )

    return sorted_ids


def topological_levels(step_ids: List[str], edges: List[Dict]) -> List[List[str]]:
    """Group step IDs into topological levels for parallel execution.

    Steps within the same level have no mutual dependencies and can
    be executed concurrently via asyncio.gather().

    Args:
        step_ids: All step IDs.
        edges: List of edge dicts with 'source' and 'target' keys.

    Returns:
        List of levels, where each level is a list of step IDs.

    Raises:
        ValueError: If the graph contains a cycle.
    """
    id_set = set(step_ids)
    adjacency: Dict[str, List[str]] = defaultdict(list)
    in_degree: Dict[str, int] = {sid: 0 for sid in step_ids}

    for edge in edges:
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src in id_set and tgt in id_set:
            adjacency[src].append(tgt)
            in_degree[tgt] = in_degree.get(tgt, 0) + 1

    # Start with all root nodes (in-degree 0)
    current_level = [sid for sid in step_ids if in_degree[sid] == 0]
    levels: List[List[str]] = []
    visited = 0

    while current_level:
        levels.append(current_level)
        visited += len(current_level)
        next_level: List[str] = []
        for node in current_level:
            for neighbor in adjacency[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_level.append(neighbor)
        current_level = next_level

    if visited != len(step_ids):
        remaining = set(step_ids) - {sid for level in levels for sid in level}
        raise ValueError(
            f"DAG contains a cycle involving nodes: {remaining}. "
            f"Grouped {visited} of {len(step_ids)} nodes into levels."
        )

    return levels


def resolve_parent_outputs(
    step_id: str,
    edges: List[Dict],
    context: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    """Resolve inputs for a step from its parent edges.

    Args:
        step_id: The target step whose inputs we're resolving.
        edges: All workflow edges.
        context: Dict mapping step_id -> {port_id: output_text}.

    Returns:
        Dict mapping target port ID -> resolved text content.
    """
    resolved: Dict[str, str] = {}

    for edge in edges:
        if edge.get("target") != step_id:
            continue

        src_id = edge.get("source", "")
        src_handle = edge.get("sourceHandle", "output")
        tgt_handle = edge.get("targetHandle", "input")

        # Look up the source step's output in the context
        src_outputs = context.get(src_id, {})
        output_text = src_outputs.get(src_handle, "")

        if output_text:
            # If multiple parents feed the same input port, concatenate
            if tgt_handle in resolved:
                resolved[tgt_handle] += f"\n\n---\n\n{output_text}"
            else:
                resolved[tgt_handle] = output_text

    return resolved


class WorkflowExecutor:
    """Executes a workflow using DAG-aware topological ordering when edges
    are present, falling back to linear index-based execution otherwise.

    Unified execution: each step runs through the same task_manager pipeline
    that powers session chat, ensuring Brain Inspector visibility and message
    persistence.
    """

    def __init__(self):
        self._running: Dict[str, asyncio.Task] = {}

    # ─── Main Entry Point ────────────────────────────────

    async def execute_workflow(
        self,
        run_id: str,
        workflow: Dict[str, Any],
        session_id: str,
        on_log: Optional[Callable[[str], Any]] = None,
        input_prompt: Optional[str] = None,
        variables: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute all steps in a workflow.

        Automatically selects DAG or linear mode based on whether the
        workflow contains edges.

        Args:
            run_id: The workflow_run record ID
            workflow: Full workflow definition (id, name, steps[], edges[], config)
            session_id: Session to execute under (shared workspace)
            on_log: Optional callback for streaming log messages
            input_prompt: Optional user-provided prompt injected into root steps
            variables: Optional resolved variable dict for ${VAR} substitution
        """
        steps = workflow.get("steps", [])
        edges = workflow.get("edges", [])

        if not steps:
            update_run(run_id, status="completed", results=[])
            return {"status": "completed", "results": []}

        # Decide execution mode
        if edges:
            return await self._execute_dag(
                run_id, workflow, session_id, on_log,
                input_prompt=input_prompt, variables=variables,
            )
        else:
            return await self._execute_linear(
                run_id, workflow, session_id, on_log,
                input_prompt=input_prompt, variables=variables,
            )

    # ─── DAG Execution (Parallel by Level) ─────────────────

    async def _execute_dag(
        self,
        run_id: str,
        workflow: Dict[str, Any],
        session_id: str,
        on_log: Optional[Callable[[str], Any]] = None,
        input_prompt: Optional[str] = None,
        variables: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute workflow in DAG mode with level-based parallelism.

        Steps are grouped into topological levels. Steps within the same
        level have no mutual dependencies and execute concurrently via
        asyncio.gather(). Levels are processed sequentially — level N+1
        starts only after all tasks in level N complete.
        """
        steps = workflow.get("steps", [])
        edges = workflow.get("edges", [])
        workflow_name = workflow.get("name", "Untitled Workflow")
        workspace = get_session_workspace(session_id)

        # Build step lookup
        step_map: Dict[str, Dict] = {s.get("id", f"step_{i}"): s for i, s in enumerate(steps)}
        step_ids = list(step_map.keys())

        # Log workflow start
        mode_info = f"DAG mode (parallel), {len(edges)} edges"
        start_msg = f"▶ Running workflow: **{workflow_name}** ({len(steps)} steps, {mode_info})"
        if input_prompt:
            start_msg += f"\n\n**Input:** {input_prompt[:500]}"
        if variables:
            var_display = ", ".join(f"`{k}`={v[:50]}" for k, v in variables.items())
            start_msg += f"\n**Variables:** {var_display}"
        add_message(session_id, source='user', content=start_msg, agent_type='workflow')
        session_events.emit_event(
            session_id, EventType.AGENT_SELECTED,
            agent="workflow",
            metadata={
                "workflow_id": workflow.get("id"),
                "workflow_name": workflow_name,
                "total_steps": len(steps),
                "run_id": run_id,
                "has_input_prompt": bool(input_prompt),
                "execution_mode": "dag_parallel",
                "edge_count": len(edges),
            },
        )

        # Build topological levels
        try:
            levels = topological_levels(step_ids, edges)
        except ValueError as e:
            error_msg = f"DAG validation failed: {e}"
            if on_log:
                await on_log(f"\n🔴 {error_msg}\n")
            add_message(session_id, source='agent', content=f"🔴 {error_msg}", agent_type='workflow')
            update_run(run_id, status="failed", error=error_msg, results=[])
            return {"status": "failed", "results": [], "error": error_msg}

        if on_log:
            level_desc = " → ".join(
                f"[{', '.join(step_map[sid].get('name', sid)[:15] for sid in lvl)}]"
                for lvl in levels
            )
            await on_log(f"📊 Execution levels ({len(levels)}): {level_desc}\n")

        # Identify root nodes (no incoming edges from other steps)
        nodes_with_parents = {e.get("target") for e in edges if e.get("target") in step_map}
        root_nodes = set(step_ids) - nodes_with_parents

        # Context: stores each step's outputs keyed by {step_id: {port_id: text}}
        context: Dict[str, Dict[str, str]] = {}
        results: List[Dict[str, Any]] = []
        completed_count = 0  # track for progress reporting

        try:
            for level_idx, level_step_ids in enumerate(levels):
                # Check if run was cancelled before starting this level
                run = get_run(run_id)
                if run and run["status"] == "cancelled":
                    if on_log:
                        await on_log(f"\n⛔ Workflow cancelled at level {level_idx + 1}")
                    cancel_msg = f"⛔ Workflow cancelled at level {level_idx + 1}/{len(levels)}"
                    add_message(session_id, source='agent', content=cancel_msg, agent_type='workflow')
                    update_run(run_id, status="cancelled", current_step=completed_count,
                               executing_steps=[], results=results)
                    return {"status": "cancelled", "results": results}

                is_single = len(level_step_ids) == 1
                level_label = f"Level {level_idx + 1}/{len(levels)}"
                if on_log:
                    names = [step_map[sid].get("name", sid)[:20] for sid in level_step_ids]
                    parallel_tag = "" if is_single else f" ⚡ PARALLEL ({len(level_step_ids)} tasks)"
                    await on_log(f"\n{'═' * 60}\n{level_label}: {', '.join(names)}{parallel_tag}\n{'═' * 60}\n")

                # Mark these steps as executing
                update_run(run_id, current_step=completed_count,
                           executing_steps=level_step_ids, results=results)

                # ─── Execute all steps in this level concurrently ─────
                async def execute_one_dag_step(step_id: str) -> Dict[str, Any]:
                    """Execute a single DAG step, returning its result dict."""
                    return await self._execute_dag_step(
                        step_id=step_id,
                        step_map=step_map,
                        step_ids=step_ids,
                        edges=edges,
                        context=context,
                        root_nodes=root_nodes,
                        run_id=run_id,
                        session_id=session_id,
                        workspace=workspace,
                        workflow_name=workflow_name,
                        exec_idx=completed_count + level_step_ids.index(step_id),
                        total_steps=len(step_ids),
                        on_log=on_log,
                        input_prompt=input_prompt,
                        variables=variables,
                        results_so_far=results,
                    )

                # Fan-out: run all steps in this level concurrently
                level_results = await asyncio.gather(
                    *(execute_one_dag_step(sid) for sid in level_step_ids),
                    return_exceptions=True,
                )

                # ─── Fan-in: collect results ─────
                level_failed = False
                for sid, step_result in zip(level_step_ids, level_results):
                    if isinstance(step_result, Exception):
                        # asyncio.gather caught an exception
                        error_msg = f"Step {step_map[sid].get('name', sid)} crashed: {step_result}"
                        step_result = {
                            "step_id": sid,
                            "step_index": step_ids.index(sid),
                            "agent": step_map[sid].get("agent", "unknown"),
                            "status": "error",
                            "error": error_msg,
                            "started_at": int(time.time() * 1000),
                            "finished_at": int(time.time() * 1000),
                        }

                    results.append(step_result)

                    # Store output in DAG context for downstream steps
                    output_text = step_result.get("output", "")
                    step = step_map[sid]
                    step_outputs = step.get("outputs", [{"id": "output"}])
                    context[sid] = {
                        port.get("id", "output"): output_text
                        for port in step_outputs
                    }

                    # Track failures
                    if step_result.get("status") in ("error", "timeout", "rate_limited"):
                        step_config = step.get("config", {})
                        if not step_config.get("continue_on_error", False):
                            level_failed = True

                completed_count += len(level_step_ids)

                # Clear executing_steps after level completes
                update_run(run_id, current_step=completed_count,
                           executing_steps=[], results=results)

                # Stop if any step in this level failed (and wasn't set to continue)
                if level_failed:
                    is_rate_limited = any(r.get("status") == "rate_limited" for r in results)
                    first_error = next(
                        (r.get("error", "Unknown error") for r in results
                         if r.get("status") in ("error", "timeout", "rate_limited")),
                        "Step failed"
                    )
                    final_status = "rate_limited" if is_rate_limited else "failed"
                    update_run(run_id, status=final_status, results=results, error=first_error)
                    return {"status": final_status, "results": results, "error": first_error}

            # All levels completed
            completion_msg = (
                f"✅ Workflow **{workflow_name}** completed — "
                f"{len(results)} steps across {len(levels)} levels (parallel DAG)"
            )
            add_message(session_id, source='agent', content=completion_msg, agent_type='workflow')
            session_events.emit_event(
                session_id, EventType.TOOL_RESULT,
                content=completion_msg, agent="workflow",
                metadata={"run_id": run_id, "total_steps": len(steps), "levels": len(levels)},
            )
            update_run(run_id, status="completed", current_step=len(steps),
                       executing_steps=[], results=results)
            if on_log:
                await on_log(f"\n🎉 Workflow completed (parallel DAG) — {len(results)} steps, {len(levels)} levels\n")
            return {"status": "completed", "results": results}

        except Exception as e:
            error_msg = f"Workflow error: {str(e)}"
            add_message(session_id, source='agent', content=f"💥 {error_msg}", agent_type='workflow')
            session_events.emit_event(
                session_id, EventType.ERROR,
                content=error_msg, agent="workflow",
                metadata={"run_id": run_id},
            )
            update_run(run_id, status="failed", executing_steps=[], results=results, error=str(e))
            return {"status": "failed", "results": results, "error": str(e)}

    # ─── Single DAG Step Execution (used by parallel gather) ─────

    async def _execute_dag_step(
        self,
        step_id: str,
        step_map: Dict[str, Dict],
        step_ids: List[str],
        edges: List[Dict],
        context: Dict[str, Dict[str, str]],
        root_nodes: set,
        run_id: str,
        session_id: str,
        workspace: str,
        workflow_name: str,
        exec_idx: int,
        total_steps: int,
        on_log: Optional[Callable[[str], Any]],
        input_prompt: Optional[str],
        variables: Optional[Dict[str, str]],
        results_so_far: List[Dict],
    ) -> Dict[str, Any]:
        """Execute a single step within a DAG level.

        This method is designed to be called concurrently via asyncio.gather().
        It resolves parent inputs from the shared context dict (which is
        safe because parent levels have already completed).
        """
        step = step_map[step_id]
        step_index = step_ids.index(step_id)
        agent = step.get("agent", "gemini")
        prompt = step.get("prompt", "")
        step_name = step.get("name") or step_id
        step_config = step.get("config", {})
        skills = step.get("skills", [])
        input_files = step.get("inputFiles") or step.get("input_files") or []

        # Substitute ${VAR_NAME} variables
        if variables:
            prompt = self._substitute_variables(prompt, variables)

        # ─── Resolve inputs from parent edges ─────
        parent_inputs = resolve_parent_outputs(step_id, edges, context)
        combined_parent_output = "\n\n".join(parent_inputs.values()) if parent_inputs else ""

        # Determine prev_exit_code from parent results
        parent_step_ids = {e.get("source") for e in edges if e.get("target") == step_id}
        prev_exit_code = 0
        for r in results_so_far:
            if r.get("step_id") in parent_step_ids and r.get("exit_code"):
                prev_exit_code = r["exit_code"]

        # ─── Evaluate Condition ─────
        condition = step.get("condition") or step.get("config", {}).get("condition")
        if condition and condition.get("type") != "always":
            should_run, reason = self._evaluate_condition(
                condition, combined_parent_output, prev_exit_code, workspace
            )

            if not should_run:
                skip_msg = f"⏭️ Node {step_name} skipped: {reason}"
                if on_log:
                    await on_log(f"\n{skip_msg}\n")
                add_message(session_id, source='agent', content=skip_msg, agent_type='workflow')

                # Store empty output so downstream nodes can still resolve
                context[step_id] = {"output": ""}

                return {
                    "step_id": step_id,
                    "step_index": step_index,
                    "agent": agent,
                    "status": "skipped",
                    "output": reason,
                    "started_at": int(time.time() * 1000),
                    "finished_at": int(time.time() * 1000),
                }

        if on_log:
            parent_info = f" (inputs from: {', '.join(parent_step_ids)})" if parent_step_ids else " (root node)"
            await on_log(f"\n─── [{exec_idx + 1}/{total_steps}] {step_name} ({agent}){parent_info} ───\n")

        # ─── Sub-Workflow Execution ─────
        if agent == "sub_workflow":
            sub_result = await self._execute_sub_workflow(
                step, step_id, step_index, run_id, session_id,
                on_log, combined_parent_output, variables, results_so_far,
            )
            if sub_result:
                context[step_id] = {"output": sub_result.get("output", "")}
                return sub_result
            # Shouldn't happen, but return error if sub_result is None
            return {
                "step_id": step_id, "step_index": step_index,
                "agent": agent, "status": "error",
                "error": "Sub-workflow returned no result",
                "started_at": int(time.time() * 1000),
                "finished_at": int(time.time() * 1000),
            }

        # ─── Build effective prompt with DAG context ─────
        effective_prompt = self._build_dag_prompt(
            prompt=prompt,
            parent_inputs=parent_inputs,
            skills=skills,
            input_files=input_files,
            workspace=workspace,
            is_root=step_id in root_nodes,
            input_prompt=input_prompt if step_id in root_nodes else None,
        )

        # Execute the step (reuses the unified execution logic)
        step_result = await self._execute_step(
            step=step,
            step_id=step_id,
            step_index=step_index,
            exec_idx=exec_idx,
            total_steps=total_steps,
            agent=agent,
            prompt=prompt,
            effective_prompt=effective_prompt,
            step_name=step_name,
            step_config=step_config,
            run_id=run_id,
            session_id=session_id,
            workspace=workspace,
            workflow_name=workflow_name,
            on_log=on_log,
        )

        return step_result

    # ─── Linear Execution (Backward-Compatible) ──────────

    async def _execute_linear(
        self,
        run_id: str,
        workflow: Dict[str, Any],
        session_id: str,
        on_log: Optional[Callable[[str], Any]] = None,
        input_prompt: Optional[str] = None,
        variables: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute workflow in linear mode (original index-based loop)."""
        steps = workflow.get("steps", [])
        workflow_name = workflow.get("name", "Untitled Workflow")
        workspace = get_session_workspace(session_id)
        results: List[Dict[str, Any]] = []
        prev_output = ""

        # Log workflow start
        start_msg = f"▶ Running workflow: **{workflow_name}** ({len(steps)} steps, linear mode)"
        if input_prompt:
            start_msg += f"\n\n**Input:** {input_prompt[:500]}"
        if variables:
            var_display = ", ".join(f"`{k}`={v[:50]}" for k, v in variables.items())
            start_msg += f"\n**Variables:** {var_display}"
        add_message(session_id, source='user', content=start_msg, agent_type='workflow')
        session_events.emit_event(
            session_id, EventType.AGENT_SELECTED,
            agent="workflow",
            metadata={
                "workflow_id": workflow.get("id"),
                "workflow_name": workflow_name,
                "total_steps": len(steps),
                "run_id": run_id,
                "has_input_prompt": bool(input_prompt),
                "execution_mode": "linear",
            },
        )

        try:
            i = 0
            prev_exit_code = 0
            while i < len(steps):
                step = steps[i]

                # Check if run was cancelled
                run = get_run(run_id)
                if run and run["status"] == "cancelled":
                    if on_log:
                        await on_log(f"\n⛔ Workflow cancelled at step {i + 1}")
                    cancel_msg = f"⛔ Workflow cancelled at step {i + 1}/{len(steps)}"
                    add_message(session_id, source='agent', content=cancel_msg, agent_type='workflow')
                    update_run(run_id, status="cancelled", current_step=i, results=results)
                    return {"status": "cancelled", "results": results}

                step_id = step.get("id", f"step_{i}")
                agent = step.get("agent", "gemini")
                prompt = step.get("prompt", "")
                step_name = step.get("name") or step_id or f"Step {i + 1}"
                step_config = step.get("config", {})
                skills = step.get("skills", [])
                input_files = step.get("inputFiles") or step.get("input_files") or []

                # Substitute ${VAR_NAME} variables in prompt
                if variables:
                    prompt = self._substitute_variables(prompt, variables)

                # ─── Evaluate Condition (Conditional Branching) ─────
                condition = step.get("condition") or step.get("config", {}).get("condition")
                if condition and condition.get("type") != "always":
                    should_run, reason = self._evaluate_condition(
                        condition, prev_output, prev_exit_code, workspace
                    )

                    if not should_run:
                        skip_action = condition.get("on_false", "skip")
                        skip_msg = f"⏭️ Step {i + 1} ({step_name}) skipped: {reason}"

                        if on_log:
                            await on_log(f"\n{skip_msg}\n")
                        add_message(session_id, source='agent', content=skip_msg, agent_type='workflow')

                        results.append({
                            "step_id": step_id,
                            "step_index": i,
                            "agent": agent,
                            "status": "skipped",
                            "output": reason,
                            "started_at": int(time.time() * 1000),
                            "finished_at": int(time.time() * 1000),
                        })

                        if skip_action == "stop":
                            update_run(run_id, status="completed", current_step=i, results=results)
                            return {"status": "completed", "results": results}
                        elif skip_action == "goto":
                            goto_target = condition.get("goto_step", "")
                            target_idx = self._find_step_index(steps, goto_target)
                            if target_idx is not None and target_idx > i:
                                i = target_idx
                                continue
                            i += 1
                            continue
                        else:
                            i += 1
                            continue

                if on_log:
                    await on_log(f"\n═══ Step {i + 1}/{len(steps)}: {step_name} ({agent}) ═══\n")

                update_run(run_id, current_step=i, results=results)

                # ─── Sub-Workflow Execution ─────
                if agent == "sub_workflow":
                    sub_result = await self._execute_sub_workflow(
                        step, step_id, i, run_id, session_id,
                        on_log, prev_output, variables, results,
                    )
                    if sub_result:
                        results.append(sub_result)
                        if sub_result.get("output"):
                            prev_output = sub_result["output"]
                        if sub_result["status"] == "error" and not step_config.get("continue_on_error", False):
                            update_run(run_id, status="failed", results=results, error=sub_result.get("error"))
                            return {"status": "failed", "results": results, "error": sub_result.get("error")}
                    i += 1
                    continue

                # Build the effective prompt (linear mode)
                effective_prompt = self._build_prompt(
                    prompt=prompt,
                    prev_output=prev_output,
                    skills=skills,
                    input_files=input_files,
                    workspace=workspace,
                    step_index=i,
                    input_prompt=input_prompt if i == 0 else None,
                )

                # Execute the step
                step_result = await self._execute_step(
                    step=step,
                    step_id=step_id,
                    step_index=i,
                    exec_idx=i,
                    total_steps=len(steps),
                    agent=agent,
                    prompt=prompt,
                    effective_prompt=effective_prompt,
                    step_name=step_name,
                    step_config=step_config,
                    run_id=run_id,
                    session_id=session_id,
                    workspace=workspace,
                    workflow_name=workflow_name,
                    on_log=on_log,
                )

                results.append(step_result)

                if step_result["status"] == "success":
                    prev_output = step_result.get("output", "")
                    prev_exit_code = step_result.get("exit_code", 0) or 0
                elif step_result["status"] in ("error", "timeout", "rate_limited"):
                    prev_exit_code = step_result.get("exit_code", 1) or 1
                    if not step_config.get("continue_on_error", False):
                        final_status = "rate_limited" if step_result["status"] == "rate_limited" else "failed"
                        update_run(run_id, status=final_status, results=results,
                                   error=step_result.get("error", f"Step {i + 1} failed"))
                        return {"status": final_status, "results": results}

                i += 1

            # All steps completed
            completion_msg = f"✅ Workflow **{workflow_name}** completed — {len(results)} steps"
            add_message(session_id, source='agent', content=completion_msg, agent_type='workflow')
            session_events.emit_event(
                session_id, EventType.TOOL_RESULT,
                content=completion_msg, agent="workflow",
                metadata={"run_id": run_id, "total_steps": len(steps)},
            )
            update_run(run_id, status="completed", current_step=len(steps), results=results)
            if on_log:
                await on_log(f"\n🎉 Workflow completed — {len(results)} steps\n")
            return {"status": "completed", "results": results}

        except Exception as e:
            error_msg = f"Workflow error: {str(e)}"
            add_message(session_id, source='agent', content=f"💥 {error_msg}", agent_type='workflow')
            session_events.emit_event(
                session_id, EventType.ERROR,
                content=error_msg, agent="workflow",
                metadata={"run_id": run_id},
            )
            update_run(run_id, status="failed", results=results, error=str(e))
            return {"status": "failed", "results": results, "error": str(e)}

    # ─── Unified Step Execution ──────────────────────────

    async def _execute_step(
        self,
        step: Dict,
        step_id: str,
        step_index: int,
        exec_idx: int,
        total_steps: int,
        agent: str,
        prompt: str,
        effective_prompt: str,
        step_name: str,
        step_config: Dict,
        run_id: str,
        session_id: str,
        workspace: str,
        workflow_name: str,
        on_log: Optional[Callable[[str], Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a single step via the agent hand. Returns the step result dict.

        This method is shared by both DAG and linear execution modes.
        """
        # ─── Log user message for this step ─────
        step_label = f"[Workflow Step {exec_idx + 1}/{total_steps} — {agent}]"
        add_message(
            session_id, source='user',
            content=f"{step_label}\n{prompt}",
            agent_type=agent,
        )

        # Get an available hand (fallback to others if rate limited)
        hand = hand_registry.get_available(agent, backups=["gemini", "claude", "codex"])
        if not hand:
            error_msg = f"Agent '{agent}' not available"
            if on_log:
                await on_log(f"❌ {error_msg}\n")
            add_message(session_id, source='agent', content=f"❌ {error_msg}", agent_type=agent)
            session_events.emit_event(
                session_id, EventType.ERROR, content=error_msg, agent=agent,
            )
            return {
                "step_id": step_id, "step_index": step_index,
                "agent": agent, "status": "error", "error": error_msg,
                "started_at": int(time.time() * 1000),
                "finished_at": int(time.time() * 1000),
            }

        # ─── Create a background task (unified with session execution) ─────
        bg_task = task_manager.create_task(
            session_id=session_id, agent=agent, prompt=prompt[:200],
        )
        task_id = bg_task.task_id

        # Emit session events (Brain Inspector compatible)
        session_events.emit_event(
            session_id, EventType.AGENT_SELECTED,
            agent=agent,
            metadata={
                "hand_type": hand.hand_type,
                "task_id": task_id,
                "workflow_step": step_index,
                "workflow_step_name": step_name,
                "run_id": run_id,
            },
        )
        session_events.emit_event(
            session_id, EventType.TOOL_CALL,
            content=prompt, agent=agent,
            metadata={
                "hand_type": hand.hand_type,
                "workspace": workspace,
                "task_id": task_id,
                "workflow_step": step_index,
            },
        )

        # ─── Execute via Hand ─────
        step_start = int(time.time() * 1000)
        full_output_chunks: List[str] = []

        try:
            timeout = step_config.get("timeout", DEFAULT_STEP_TIMEOUT)

            await task_manager.update_phase(task_id, TaskPhase.CONNECTING)

            first_chunk = True

            async def stream_log_with_phase(chunk: str):
                nonlocal first_chunk
                full_output_chunks.append(chunk)
                if first_chunk:
                    await task_manager.update_phase(task_id, TaskPhase.STREAMING)
                    first_chunk = False
                await task_manager.emit_output(task_id, chunk, source="agent")
                if on_log:
                    await on_log(chunk)

            await task_manager.update_phase(task_id, TaskPhase.EXECUTING)

            print(f"[Workflow:{workflow_name}] Step {exec_idx+1}/{total_steps} — {hand.name} (task={task_id})")

            result = await asyncio.wait_for(
                hand.execute(
                    effective_prompt,
                    workspace_dir=workspace,
                    on_log=stream_log_with_phase,
                ),
                timeout=timeout,
            )

            step_end = int(time.time() * 1000)
            agent_output = "".join(full_output_chunks)

            # ─── Check for Rate Limit ─────
            from app.hands.base import check_rate_limit
            wait_time = check_rate_limit(agent_output)
            is_rate_limited = False
            
            if wait_time is not None:
                is_rate_limited = True
                hand_registry.mark_rate_limited(hand.name, wait_time)
                result.exit_code = 429
                result.output = f"[RATE LIMITED] {hand.name} is paused for {wait_time}s. Original Output:\n{agent_output}"
                
                # Fetch workflow_id to schedule retry
                from app.workflow_store import get_run
                run_data = get_run(run_id)
                workflow_id = run_data.get("workflow_id") if run_data else None
                
                if workflow_id:
                    try:
                        from app.scheduler import scheduler, run_scheduled_workflow
                        from datetime import datetime, timedelta
                        run_at = datetime.now() + timedelta(seconds=wait_time + 5)
                        scheduler.add_job(
                            run_scheduled_workflow,
                            'date',
                            run_date=run_at,
                            kwargs={"workflow_id": workflow_id},
                            id=f"retry_{workflow_id}_{int(time.time())}"
                        )
                        result.output += f"\n\n[System] Scheduled automatic re-run for workflow {workflow_id} at {run_at.isoformat()}"
                    except Exception as e:
                        result.output += f"\n\n[System] Failed to schedule re-run: {e}"

            # ─── Log agent response message ─────
            add_message(
                session_id, source='agent', content=agent_output, agent_type=agent,
                image_b64=result.image_b64 if result.image_b64 else None,
            )

            # ─── Emit session events ─────
            if result.success:
                session_events.emit_event(
                    session_id, EventType.TOOL_RESULT,
                    content=result.output[:2000], agent=agent,
                    metadata={"exit_code": result.exit_code, "output_length": len(result.output),
                              "task_id": task_id, "workflow_step": step_index},
                )
            else:
                session_events.emit_event(
                    session_id, EventType.TOOL_ERROR,
                    content=result.output[:2000], agent=agent,
                    metadata={"exit_code": result.exit_code, "task_id": task_id,
                              "workflow_step": step_index},
                )

            session_events.emit_event(
                session_id, EventType.AGENT_RESPONSE,
                content=agent_output[:2000], agent=agent,
                metadata={"has_image": bool(result.image_b64), "task_id": task_id,
                          "workflow_step": step_index},
            )
            session_events.emit_event(
                session_id, EventType.METRIC, agent=agent,
                metadata={"input_tokens": len(effective_prompt) // 4,
                          "output_tokens": len(result.output) // 4,
                          "task_id": task_id, "workflow_step": step_index},
            )

            # Handle image output
            if result.image_b64:
                await task_manager.emit_event(task_id, {
                    "type": "node_execution_image", "b64": result.image_b64,
                })

            await task_manager.update_phase(task_id, TaskPhase.COMPLETED if result.success else TaskPhase.FAILED, exit_code=result.exit_code)

            step_result = {
                "step_id": step_id, "step_index": step_index,
                "agent": agent,
                "status": "success" if result.success else ("rate_limited" if is_rate_limited else "error"),
                "output": result.output,
                "exit_code": result.exit_code,
                "latency_ms": step_end - step_start,
                "started_at": step_start, "finished_at": step_end,
                "task_id": task_id,
            }
            if result.image_b64:
                step_result["has_image"] = True

            if result.success:
                if on_log:
                    await on_log(f"\n✅ Step {exec_idx + 1} completed ({(step_end - step_start) / 1000:.1f}s)\n")
            else:
                if on_log:
                    await on_log(f"\n❌ Step {exec_idx + 1} failed (exit code {result.exit_code})\n")
                step_result["error"] = f"Exit code {result.exit_code}"

            return step_result

        except asyncio.TimeoutError:
            step_end = int(time.time() * 1000)
            error_msg = f"Step {exec_idx + 1} timed out after {step_config.get('timeout', DEFAULT_STEP_TIMEOUT)}s"
            if on_log:
                await on_log(f"\n⏱️ {error_msg}\n")
            add_message(session_id, source='agent', content=f"⏱️ {error_msg}", agent_type=agent)
            session_events.emit_event(
                session_id, EventType.ERROR, content=error_msg, agent=agent,
                metadata={"task_id": task_id, "workflow_step": step_index},
            )
            await task_manager.update_phase(task_id, TaskPhase.FAILED, exit_code=1, error=error_msg)
            return {
                "step_id": step_id, "step_index": step_index,
                "agent": agent, "status": "timeout", "error": error_msg,
                "started_at": step_start, "finished_at": step_end,
                "task_id": task_id,
            }

        except Exception as e:
            step_end = int(time.time() * 1000)
            error_msg = str(e)
            if on_log:
                await on_log(f"\n💥 Step {exec_idx + 1} exception: {error_msg}\n")
            add_message(session_id, source='agent', content=f"💥 Exception: {error_msg}", agent_type=agent)
            session_events.emit_event(
                session_id, EventType.ERROR, content=error_msg, agent=agent,
                metadata={"task_id": task_id, "workflow_step": step_index},
            )
            await task_manager.update_phase(task_id, TaskPhase.FAILED, exit_code=1, error=error_msg)
            return {
                "step_id": step_id, "step_index": step_index,
                "agent": agent, "status": "error", "error": error_msg,
                "started_at": step_start, "finished_at": step_end,
                "task_id": task_id,
            }

    # ─── Sub-Workflow Execution ──────────────────────────

    async def _execute_sub_workflow(
        self,
        step: Dict,
        step_id: str,
        step_index: int,
        run_id: str,
        session_id: str,
        on_log: Optional[Callable[[str], Any]],
        parent_output: str,
        variables: Optional[Dict[str, str]],
        results: List[Dict],
    ) -> Optional[Dict[str, Any]]:
        """Execute a sub-workflow step. Returns the step result dict."""
        agent = "sub_workflow"
        step_config = step.get("config", {})
        sub_wf_id = step.get("sub_workflow_id", "")

        if not sub_wf_id:
            error_msg = "Sub-workflow step missing sub_workflow_id"
            if on_log:
                await on_log(f"❌ {error_msg}\n")
            return {
                "step_id": step_id, "step_index": step_index,
                "agent": agent, "status": "error", "error": error_msg,
                "started_at": int(time.time() * 1000),
                "finished_at": int(time.time() * 1000),
            }

        from app.workflow_store import get_workflow
        sub_workflow = get_workflow(sub_wf_id)
        if not sub_workflow:
            error_msg = f"Sub-workflow '{sub_wf_id}' not found"
            if on_log:
                await on_log(f"❌ {error_msg}\n")
            return {
                "step_id": step_id, "step_index": step_index,
                "agent": agent, "status": "error", "error": error_msg,
                "started_at": int(time.time() * 1000),
                "finished_at": int(time.time() * 1000),
            }

        if on_log:
            await on_log(f"📦 Executing sub-workflow: {sub_workflow.get('name', sub_wf_id)}\n")

        step_start = int(time.time() * 1000)
        try:
            from app.workflow_store import create_run as create_sub_run
            sub_run = create_sub_run(sub_wf_id, session_id)
            sub_result = await self.execute_workflow(
                run_id=sub_run["id"],
                workflow=sub_workflow,
                session_id=session_id,
                on_log=on_log,
                input_prompt=parent_output,
                variables=variables,
            )
            step_end = int(time.time() * 1000)
            sub_outputs = sub_result.get("results", [])
            last_sub_output = sub_outputs[-1].get("output", "") if sub_outputs else ""

            if on_log:
                await on_log(f"✅ Sub-workflow completed: {sub_result.get('status')}\n")

            return {
                "step_id": step_id, "step_index": step_index, "agent": agent,
                "status": sub_result.get("status", "completed"),
                "output": last_sub_output,
                "latency_ms": step_end - step_start,
                "started_at": step_start, "finished_at": step_end,
            }

        except Exception as e:
            step_end = int(time.time() * 1000)
            error_msg = f"Sub-workflow error: {str(e)}"
            return {
                "step_id": step_id, "step_index": step_index, "agent": agent,
                "status": "error", "error": error_msg,
                "latency_ms": step_end - step_start,
                "started_at": step_start, "finished_at": step_end,
            }

    # ─── Prompt Builders ─────────────────────────────────

    def _build_prompt(
        self,
        prompt: str,
        prev_output: str,
        skills: List[str],
        input_files: List[str],
        workspace: str,
        step_index: int,
        input_prompt: Optional[str] = None,
    ) -> str:
        """Build prompt for linear mode — simple prev_output chaining."""
        parts = []

        if input_prompt:
            parts.append(f"## User Input\n{input_prompt}\n\n---\n")

        if step_index > 0 and prev_output:
            parts.append(
                f"## Context from Previous Step\n"
                f"The previous step produced the following output:\n\n"
                f"```\n{prev_output[:8000]}\n```\n\n---\n"
            )

        parts.append(f"Working directory: {workspace}\n")

        if input_files:
            parts.append("## Input Files")
            for fpath in input_files:
                full_path = os.path.join(workspace, fpath) if not os.path.isabs(fpath) else fpath
                if os.path.isfile(full_path):
                    try:
                        with open(full_path, "r", errors="replace") as f:
                            content = f.read(4096)
                        parts.append(f"### {os.path.basename(fpath)}\n```\n{content}\n```\n")
                    except Exception:
                        parts.append(f"- {fpath} (unable to read)\n")
                else:
                    parts.append(f"- {fpath} (file not found)\n")

        if skills:
            parts.append(f"\nUse these skills/capabilities: {', '.join(skills)}\n")

        if prompt:
            parts.append(f"\n## Task\n{prompt}")

        return "\n".join(parts)

    def _build_dag_prompt(
        self,
        prompt: str,
        parent_inputs: Dict[str, str],
        skills: List[str],
        input_files: List[str],
        workspace: str,
        is_root: bool = False,
        input_prompt: Optional[str] = None,
    ) -> str:
        """Build prompt for DAG mode — inject resolved parent outputs."""
        parts = []

        # Inject user input for root nodes
        if is_root and input_prompt:
            parts.append(f"## User Input\n{input_prompt}\n\n---\n")

        # Inject resolved parent outputs
        if parent_inputs:
            parts.append("## Input Context from Connected Steps\n")
            if len(parent_inputs) == 1:
                # Single input — inline it cleanly
                port_id, content = next(iter(parent_inputs.items()))
                parts.append(
                    f"The upstream step produced the following output "
                    f"(port: `{port_id}`):\n\n"
                    f"```\n{content[:8000]}\n```\n\n---\n"
                )
            else:
                # Multiple inputs — label each
                for port_id, content in parent_inputs.items():
                    parts.append(
                        f"### Input `{port_id}`\n"
                        f"```\n{content[:4000]}\n```\n"
                    )
                parts.append("---\n")

        parts.append(f"Working directory: {workspace}\n")

        if input_files:
            parts.append("## Input Files")
            for fpath in input_files:
                full_path = os.path.join(workspace, fpath) if not os.path.isabs(fpath) else fpath
                if os.path.isfile(full_path):
                    try:
                        with open(full_path, "r", errors="replace") as f:
                            content = f.read(4096)
                        parts.append(f"### {os.path.basename(fpath)}\n```\n{content}\n```\n")
                    except Exception:
                        parts.append(f"- {fpath} (unable to read)\n")
                else:
                    parts.append(f"- {fpath} (file not found)\n")

        if skills:
            parts.append(f"\nUse these skills/capabilities: {', '.join(skills)}\n")

        if prompt:
            parts.append(f"\n## Task\n{prompt}")

        return "\n".join(parts)

    # ─── Utility Methods ─────────────────────────────────

    def cancel_run(self, run_id: str) -> bool:
        """Cancel a running workflow."""
        run = get_run(run_id)
        if run and run["status"] == "running":
            update_run(run_id, status="cancelled")
            task = self._running.pop(run_id, None)
            if task and not task.done():
                task.cancel()
            return True
        return False

    async def start_workflow(
        self,
        run_id: str,
        workflow: Dict[str, Any],
        session_id: str,
        on_log: Optional[Callable[[str], Any]] = None,
        input_prompt: Optional[str] = None,
        variables: Optional[Dict[str, str]] = None,
    ) -> asyncio.Task:
        """Start a workflow execution as a background task."""
        task = asyncio.create_task(
            self.execute_workflow(run_id, workflow, session_id, on_log,
                                 input_prompt=input_prompt, variables=variables)
        )
        self._running[run_id] = task

        def _cleanup(t):
            self._running.pop(run_id, None)

        task.add_done_callback(_cleanup)
        return task

    @staticmethod
    def _substitute_variables(text: str, variables: Dict[str, str]) -> str:
        """Replace ${VAR_NAME} placeholders in text with variable values.

        Supports both ${VAR_NAME} and $VAR_NAME syntax.
        Unresolved variables are left as-is.
        """
        if not variables:
            return text

        def replace_match(m: re.Match) -> str:
            var_name = m.group(1) or m.group(2)
            return variables.get(var_name, m.group(0))

        return re.sub(r'\$\{(\w+)\}|\$(\w+)', replace_match, text)

    @staticmethod
    def _evaluate_condition(
        condition: dict, prev_output: str, prev_exit_code: int, workspace: str
    ) -> Tuple[bool, str]:
        """Evaluate a step condition. Returns (should_run, reason).

        Condition types:
          - always: always run (default)
          - if_output_contains: run if prev output contains value
          - if_output_not_contains: run if prev output does NOT contain value
          - if_exit_code: run if prev exit code matches value
          - if_file_exists: run if file exists in workspace
        """
        cond_type = condition.get("type", "always")
        cond_value = condition.get("value", "")

        if cond_type == "always":
            return True, "always"

        if cond_type == "if_output_contains":
            found = cond_value.lower() in (prev_output or "").lower()
            if found:
                return True, f"output contains '{cond_value}'"
            return False, f"output does not contain '{cond_value}'"

        if cond_type == "if_output_not_contains":
            found = cond_value.lower() in (prev_output or "").lower()
            if not found:
                return True, f"output does not contain '{cond_value}'"
            return False, f"output contains '{cond_value}'"

        if cond_type == "if_exit_code":
            try:
                expected = int(cond_value)
            except (ValueError, TypeError):
                expected = 0
            if prev_exit_code == expected:
                return True, f"exit code is {expected}"
            return False, f"exit code is {prev_exit_code}, expected {expected}"

        if cond_type == "if_file_exists":
            filepath = os.path.join(workspace, cond_value) if workspace else cond_value
            if os.path.exists(filepath):
                return True, f"file '{cond_value}' exists"
            return False, f"file '{cond_value}' does not exist"

        return True, f"unknown condition type '{cond_type}'"

    @staticmethod
    def _find_step_index(steps: list, step_id: str) -> Optional[int]:
        """Find the index of a step by its ID. Returns None if not found."""
        for idx, step in enumerate(steps):
            if step.get("id") == step_id:
                return idx
        return None


# ─── Global Singleton ──────────────────────────────────
workflow_executor = WorkflowExecutor()
