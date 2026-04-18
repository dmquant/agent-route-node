"""Agent Orchestrator — the stateless Brain.

Key Anthropic insight: The brain is cattle, not pets.
If it crashes, wake(sessionId) + getEvents() = full recovery.

The brain's loop: observe context → decide → call hand → emit result.
It never stores state internally — everything goes through the event log.
"""

import time
import asyncio
from typing import Optional, Callable, Awaitable, List, Dict, Any

from app.hands.registry import hand_registry
from app.session.events import EventType, SessionEvent
from app.session.manager import SessionEventManager, session_events
from app.brain.harness import HarnessManager, HarnessConfig, harness_manager
from app.brain.context import ContextEngine


class AgentOrchestrator:
    """Stateless brain — routes prompts through the hand/session/context layers.

    Lifecycle:
        wake(session_id)     → Rebuild state from event log (crash recovery)
        run(session_id, ...) → Execute a single turn: observe → decide → act
        pause(session_id)    → Save checkpoint and yield control
        delegate(session_id, target) → Hand off to a sub-brain

    The orchestrator NEVER stores session state internally.
    All state is derived from getEvents(session_id).
    """

    def __init__(
        self,
        session_mgr: Optional[SessionEventManager] = None,
        harnesses: Optional[HarnessManager] = None,
    ):
        self.sessions = session_mgr or session_events
        self.harnesses = harnesses or harness_manager
        self.context = ContextEngine(self.sessions)

    # ─── Core Lifecycle ──────────────────────

    def wake(self, session_id: str) -> dict:
        """Resume a session from durable event log.

        Returns session context stats for the calling code to decide
        whether to compact, rewind, or just continue.
        """
        result = self.sessions.wake(session_id)

        # Get the harness config for the last active agent
        last_event = result.get("last_event")
        agent = (last_event or {}).get("agent", "gemini")
        harness = self.harnesses.select(agent)
        context_stats = self.context.get_context_stats(session_id, harness)

        return {
            **result,
            "context": context_stats,
            "harness": harness.to_dict(),
        }

    async def run(
        self,
        session_id: str,
        agent: str,
        prompt: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable[[str], Awaitable[None]]] = None,
        **kwargs,
    ) -> dict:
        """Execute a single turn through the brain.

        Steps:
        1. Emit user message event
        2. Select harness + resolve hand
        3. Build context window
        4. Call hand.execute()
        5. Emit result/error + metrics events
        6. Auto-checkpoint if needed

        Returns HandResult-like dict with context metadata.
        """
        start_ts = time.time()

        # 1. Emit user message
        self.sessions.emit_event(
            session_id, EventType.USER_MESSAGE,
            content=prompt, agent="user",
        )

        # 2. Select harness and resolve hand
        harness = self.harnesses.select(agent)
        hand = hand_registry.get(agent)
        if not hand:
            self.sessions.emit_event(
                session_id, EventType.ERROR,
                content=f"No hand registered: {agent}",
                agent=agent,
            )
            return {
                "success": False,
                "output": f"No hand registered for '{agent}'",
                "exit_code": 1,
                "context": {},
            }

        self.sessions.emit_event(
            session_id, EventType.AGENT_SELECTED,
            agent=agent,
            metadata={"hand_type": hand.hand_type, "harness": harness.agent},
        )

        # 3. Build context window
        context_window = self.context.build_context(session_id, harness)
        if context_window.get("compacted"):
            self.sessions.emit_event(
                session_id, EventType.CONTEXT_COMPACT,
                content=f"Context compacted: {context_window.get('strategy')}",
                agent=agent,
                metadata={
                    "strategy": context_window.get("strategy"),
                    "dropped": context_window.get("dropped_events", 0),
                },
            )

        # 4. Emit tool call and execute
        self.sessions.emit_event(
            session_id, EventType.TOOL_CALL,
            content=prompt, agent=agent,
            metadata={
                "hand_type": hand.hand_type,
                "workspace": workspace_dir,
                "context_tokens": context_window.get("estimated_tokens", 0),
            },
        )

        try:
            result = await hand.execute(
                prompt,
                workspace_dir=workspace_dir,
                on_log=on_log,
                **kwargs,
            )
        except Exception as e:
            self.sessions.emit_event(
                session_id, EventType.TOOL_ERROR,
                content=str(e), agent=agent,
                metadata={"exception": type(e).__name__},
            )

            # Retry logic
            if harness.retry_on_failure and harness.max_retries > 0:
                return await self._retry(
                    session_id, agent, prompt, workspace_dir,
                    on_log, harness, attempt=1, last_error=str(e),
                    **kwargs,
                )

            return {
                "success": False,
                "output": f"Hand execution failed: {e}",
                "exit_code": 1,
                "context": context_window,
            }

        # 5. Emit result + metrics
        elapsed_ms = int((time.time() - start_ts) * 1000)

        if result.success:
            self.sessions.emit_event(
                session_id, EventType.TOOL_RESULT,
                content=result.output[:2000],
                agent=agent,
                metadata={
                    "exit_code": result.exit_code,
                    "output_length": len(result.output),
                    "latency_ms": elapsed_ms,
                },
            )
        else:
            self.sessions.emit_event(
                session_id, EventType.TOOL_ERROR,
                content=result.output[:2000],
                agent=agent,
                metadata={"exit_code": result.exit_code, "latency_ms": elapsed_ms},
            )

        # Token metrics
        self.sessions.emit_event(
            session_id, EventType.METRIC,
            agent=agent,
            metadata={
                "input_tokens": len(prompt) // 4,
                "output_tokens": len(result.output) // 4,
                "latency_ms": elapsed_ms,
                "context_tokens": context_window.get("estimated_tokens", 0),
            },
        )

        # Agent response
        self.sessions.emit_event(
            session_id, EventType.AGENT_RESPONSE,
            content=result.output[:2000],
            agent=agent,
            metadata={"has_image": bool(result.image_b64)},
        )

        # 6. Auto-checkpoint (every 10 turns or after long sessions)
        event_count = self.sessions.get_event_count(session_id)
        if event_count > 0 and event_count % 50 == 0:
            self.sessions.checkpoint(session_id, f"Auto-checkpoint at {event_count} events")

        return {
            "success": result.success,
            "output": result.output,
            "exit_code": result.exit_code,
            "image_b64": result.image_b64,
            "context": {
                "strategy": context_window.get("strategy"),
                "estimated_tokens": context_window.get("estimated_tokens"),
                "compacted": context_window.get("compacted", False),
            },
            "metrics": {
                "latency_ms": elapsed_ms,
                "event_count": event_count,
            },
        }

    async def _retry(
        self,
        session_id: str,
        agent: str,
        prompt: str,
        workspace_dir: str,
        on_log: Optional[Callable],
        harness: HarnessConfig,
        attempt: int,
        last_error: str,
        **kwargs,
    ) -> dict:
        """Retry a failed hand execution with backoff."""
        if attempt > harness.max_retries:
            return {
                "success": False,
                "output": f"Max retries ({harness.max_retries}) exceeded. Last error: {last_error}",
                "exit_code": 1,
                "context": {},
            }

        backoff = min(2 ** attempt, 30)
        self.sessions.emit_event(
            session_id, EventType.METRIC,
            agent=agent,
            metadata={"retry_attempt": attempt, "backoff_seconds": backoff},
        )

        await asyncio.sleep(backoff)

        hand = hand_registry.get(agent)
        if not hand:
            return {
                "success": False,
                "output": f"Hand disappeared during retry: {agent}",
                "exit_code": 1,
                "context": {},
            }

        try:
            result = await hand.execute(prompt, workspace_dir=workspace_dir, on_log=on_log, **kwargs)
            self.sessions.emit_event(
                session_id, EventType.TOOL_RESULT,
                content=result.output[:2000], agent=agent,
                metadata={"exit_code": result.exit_code, "retry_attempt": attempt},
            )
            return {
                "success": result.success,
                "output": result.output,
                "exit_code": result.exit_code,
                "image_b64": result.image_b64,
                "context": {},
                "metrics": {"retry_attempts": attempt},
            }
        except Exception as e:
            return await self._retry(
                session_id, agent, prompt, workspace_dir,
                on_log, harness, attempt + 1, str(e), **kwargs,
            )

    def pause(self, session_id: str, summary: str = "") -> str:
        """Save checkpoint and yield control."""
        self.sessions.emit_event(session_id, EventType.SESSION_PAUSED)
        return self.sessions.checkpoint(
            session_id, summary or "Brain paused"
        )

    async def delegate(
        self,
        session_id: str,
        from_agent: str,
        to_agent: str,
        prompt: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable] = None,
        **kwargs,
    ) -> dict:
        """Delegate execution from one agent to another.

        Emits delegation events for traceability, then runs through
        the target hand. The original caller can see the result
        via the event log.
        """
        self.sessions.emit_event(
            session_id, EventType.AGENT_DELEGATED,
            content=f"Delegating from {from_agent} to {to_agent}",
            agent=from_agent,
            metadata={"target_agent": to_agent, "prompt_preview": prompt[:100]},
        )

        result = await self.run(
            session_id, to_agent, prompt,
            workspace_dir=workspace_dir, on_log=on_log, **kwargs,
        )

        self.sessions.emit_event(
            session_id, EventType.AGENT_JOINED,
            content=f"Delegation from {to_agent} complete",
            agent=to_agent,
            metadata={
                "success": result.get("success"),
                "delegated_from": from_agent,
            },
        )

        return result

    def get_brain_status(self, session_id: str) -> dict:
        """Get orchestrator status for a session (debugging/UI)."""
        summary = self.sessions.get_session_summary(session_id)
        tokens = self.sessions.get_token_usage(session_id)
        latest = self.sessions.get_latest_event(session_id)

        last_agent = (latest.agent if latest else None) or "none"
        harness = self.harnesses.select(last_agent)
        context_stats = self.context.get_context_stats(session_id, harness)

        return {
            "session_id": session_id,
            "brain_state": "active" if latest else "idle",
            "last_agent": last_agent,
            "event_summary": summary,
            "token_usage": tokens,
            "context": context_stats,
            "harness": harness.to_dict(),
        }

    # ─── Multi-Agent Delegation (Phase 9) ──────────────────────

    async def fan_out(
        self,
        session_id: str,
        agents: List[str],
        prompt: str,
        workspace_dir: str = "/tmp",
        on_log: Optional[Callable] = None,
        timeout: float = 300.0,
        **kwargs,
    ) -> List[dict]:
        """Dispatch the same prompt to multiple agents in parallel.

        Returns a list of results from each agent, in the same order
        as the input `agents` list. Failed agents return error results.

        Usage:
            results = await orchestrator.fan_out(
                session_id, ["gemini", "claude"], "Analyze this code",
                workspace_dir="/path/to/workspace"
            )
        """
        self.sessions.emit_event(
            session_id, EventType.AGENT_DELEGATED,
            content=f"Fan-out to {len(agents)} agents: {', '.join(agents)}",
            agent="orchestrator",
            metadata={"agents": agents, "pattern": "fan_out", "timeout": timeout},
        )

        # Per-agent workspace isolation: each agent gets its own subdirectory
        import os
        for agent_name in agents:
            agent_workspace = os.path.join(workspace_dir, f"_fanout_{agent_name}")
            os.makedirs(agent_workspace, exist_ok=True)

        async def run_one(agent_name: str) -> dict:
            # Isolate each agent to its own workspace subdirectory
            agent_workspace = os.path.join(workspace_dir, f"_fanout_{agent_name}")
            try:
                result = await asyncio.wait_for(
                    self.run(
                        session_id, agent_name, prompt,
                        workspace_dir=agent_workspace,
                        on_log=on_log, **kwargs,
                    ),
                    timeout=timeout,
                )
                return {**result, "agent": agent_name, "workspace": agent_workspace}
            except asyncio.TimeoutError:
                self.sessions.emit_event(
                    session_id, EventType.TOOL_ERROR,
                    content=f"Agent {agent_name} timed out after {timeout}s",
                    agent=agent_name,
                    metadata={"timeout": timeout, "pattern": "fan_out"},
                )
                return {
                    "success": False,
                    "output": f"Agent {agent_name} timed out after {timeout}s",
                    "exit_code": 124,
                    "agent": agent_name,
                    "context": {},
                }
            except Exception as e:
                return {
                    "success": False,
                    "output": f"Agent {agent_name} failed: {e}",
                    "exit_code": 1,
                    "agent": agent_name,
                    "context": {},
                }

        # Fire all agents in parallel
        tasks = [asyncio.create_task(run_one(a)) for a in agents]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        self.sessions.emit_event(
            session_id, EventType.METRIC,
            agent="orchestrator",
            metadata={
                "pattern": "fan_out",
                "agents": agents,
                "success_count": sum(1 for r in results if r.get("success")),
                "total": len(agents),
            },
        )

        return list(results)

    def join(
        self,
        session_id: str,
        results: List[dict],
        strategy: str = "all",
    ) -> dict:
        """Merge results from a fan_out using a strategy.

        Strategies:
            "all"           — Return all results (no filtering)
            "first_success" — Return the first successful result
            "majority_vote" — Return the majority output (by exit code)
            "best_effort"   — Return successful results, fallback to any
        """
        self.sessions.emit_event(
            session_id, EventType.AGENT_JOINED,
            content=f"Joining {len(results)} results with strategy: {strategy}",
            agent="orchestrator",
            metadata={
                "strategy": strategy,
                "agents": [r.get("agent", "unknown") for r in results],
                "outcomes": [r.get("success", False) for r in results],
            },
        )

        if strategy == "first_success":
            for r in results:
                if r.get("success"):
                    return {
                        "success": True,
                        "output": r["output"],
                        "exit_code": r.get("exit_code", 0),
                        "selected_agent": r.get("agent"),
                        "all_results": results,
                    }
            # All failed — return first result
            return {
                "success": False,
                "output": results[0]["output"] if results else "No results",
                "exit_code": 1,
                "all_results": results,
            }

        elif strategy == "majority_vote":
            success_count = sum(1 for r in results if r.get("success"))
            return {
                "success": success_count > len(results) / 2,
                "output": "\n---\n".join(
                    f"[{r.get('agent', '?')}] {r.get('output', '')[:500]}"
                    for r in results
                ),
                "exit_code": 0 if success_count > len(results) / 2 else 1,
                "votes": {"success": success_count, "failed": len(results) - success_count},
                "all_results": results,
            }

        elif strategy == "best_effort":
            successes = [r for r in results if r.get("success")]
            if successes:
                return {
                    "success": True,
                    "output": "\n---\n".join(
                        f"[{r.get('agent', '?')}] {r.get('output', '')[:500]}"
                        for r in successes
                    ),
                    "exit_code": 0,
                    "successful_agents": [r.get("agent") for r in successes],
                    "all_results": results,
                }
            return {
                "success": False,
                "output": "All agents failed",
                "exit_code": 1,
                "all_results": results,
            }

        else:  # "all"
            return {
                "success": all(r.get("success") for r in results),
                "output": "\n---\n".join(
                    f"[{r.get('agent', '?')}] {r.get('output', '')[:500]}"
                    for r in results
                ),
                "exit_code": 0 if all(r.get("success") for r in results) else 1,
                "all_results": results,
            }

    async def multi_agent_run(
        self,
        session_id: str,
        agents: List[str],
        prompt: str,
        workspace_dir: str = "/tmp",
        strategy: str = "first_success",
        timeout: float = 300.0,
        on_log: Optional[Callable] = None,
        **kwargs,
    ) -> dict:
        """High-level multi-agent execution: fan_out + join.

        Dispatches the prompt to all agents in parallel, waits for results,
        then merges using the selected strategy.

        Usage:
            result = await orchestrator.multi_agent_run(
                session_id, ["gemini", "claude"],
                "Write unit tests for auth.py",
                workspace_dir="/project",
                strategy="first_success",
            )
        """
        fan_results = await self.fan_out(
            session_id, agents, prompt,
            workspace_dir=workspace_dir,
            on_log=on_log, timeout=timeout,
            **kwargs,
        )

        merged = self.join(session_id, fan_results, strategy=strategy)

        self.sessions.emit_event(
            session_id, EventType.AGENT_RESPONSE,
            content=f"Multi-agent run complete ({strategy}): {len(agents)} agents",
            agent="orchestrator",
            metadata={
                "pattern": "multi_agent_run",
                "strategy": strategy,
                "final_success": merged.get("success"),
                "agents": agents,
            },
        )

        return merged


# ─── Global Singleton ──────────────────────
orchestrator = AgentOrchestrator()

