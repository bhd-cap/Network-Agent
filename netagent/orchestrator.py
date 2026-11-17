"""Task queue, routing and agent execution."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .agent import build_system_prompt
from .db import now
from .ladder import LadderError, run_ladder
from .tools import ToolContext, tools_for

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

log = logging.getLogger("netagent.orchestrator")

TERMINAL = {"completed", "failed", "cancelled"}
PRIORITIES = {1: "urgent", 2: "high", 3: "normal", 4: "low"}


class Orchestrator:
    def __init__(self, rt: "Runtime"):
        self.rt = rt
        self.db = rt.db
        self._wake = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}
        self._agent_load: dict[int, set[int]] = {}
        self._stopping = False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._recover()
        self._loop_task = asyncio.create_task(self._loop(), name="netagent-dispatcher")

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._running.values())
        if self._loop_task:
            tasks.append(self._loop_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Anything interrupted by shutdown goes back on the queue for the next start.
        self._recover()

    def _recover(self) -> None:
        for task in self.db.query("SELECT id, source_ref FROM tasks WHERE status = 'running'"):
            if (task["source_ref"] or {}).get("inline"):
                self.db.update("tasks", task["id"], {
                    "status": "failed", "error": "Interrupted by a restart (the parent task is re-run).",
                    "completed_at": now(),
                })
            else:
                self.db.update("tasks", task["id"], {"status": "queued", "started_at": None})
                self.add_event(task["id"], "status", "Re-queued after a service restart")

    def kick(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                self._dispatch()
            except Exception:  # keep the dispatcher alive whatever happens
                log.exception("dispatch failed")

    def _dispatch(self) -> None:
        # Tasks that arrived when no agent could take them get another routing attempt.
        for task in self.db.query("SELECT * FROM tasks WHERE status = 'queued' AND agent_id IS NULL ORDER BY id LIMIT 20"):
            agent_id = self.route(task["title"], task["description"])
            if agent_id:
                self.db.update("tasks", task["id"], {"agent_id": agent_id})
                self.add_event(task["id"], "status", f"Assigned to {self._agent_name(agent_id)}")
        for agent in self.db.query("SELECT * FROM agents WHERE status = 'active'"):
            load = self._agent_load.setdefault(agent["id"], set())
            free = int(agent["max_parallel"] or 1) - len(load)
            if free <= 0:
                continue
            queued = self.db.query(
                "SELECT id FROM tasks WHERE status = 'queued' AND agent_id = ? ORDER BY priority, id LIMIT ?",
                [agent["id"], free],
            )
            for row in queued:
                task_id = row["id"]
                self.db.update("tasks", task_id, {"status": "running", "started_at": now()})
                load.add(task_id)
                runner = asyncio.create_task(self._execute(task_id), name=f"task-{task_id}")
                self._running[task_id] = runner
                runner.add_done_callback(lambda _t, tid=task_id, aid=agent["id"]: self._finished(tid, aid))

    def _finished(self, task_id: int, agent_id: int) -> None:
        self._running.pop(task_id, None)
        self._agent_load.get(agent_id, set()).discard(task_id)
        self.kick()

    # ------------------------------------------------------------------
    # task API
    # ------------------------------------------------------------------
    def add_event(self, task_id: int, kind: str, summary: str, data: dict | None = None) -> None:
        self.db.insert("task_events", {
            "task_id": task_id, "ts": now(), "kind": kind, "summary": summary[:2000], "data": data or {},
        })
        self.rt.bus.publish("task.event", task_id=task_id, event=kind)

    def _agent_name(self, agent_id: int | None) -> str:
        if not agent_id:
            return "unassigned"
        row = self.db.get("agents", agent_id)
        return row["name"] if row else f"agent {agent_id}"

    def route(self, title: str, description: str) -> int | None:
        """Pick an agent for an unaddressed task.

        One specialist profile matches the text -> its least-busy agent. Several specialist profiles match
        (a multi-vendor request) or none do -> the coordinator. No coordinator -> the best match, else the first agent.
        """
        agents = self.db.query(
            "SELECT a.id, a.name, a.profile_id, p.keywords, p.coordinator, "
            "(SELECT COUNT(*) FROM tasks t WHERE t.agent_id = a.id AND t.status IN ('queued','running')) AS load "
            "FROM agents a JOIN profiles p ON p.id = a.profile_id WHERE a.status = 'active' ORDER BY a.id"
        )
        if not agents:
            return None
        if len(agents) == 1:
            return agents[0]["id"]
        text = f" {title} {description} ".lower()

        def hits(agent: dict) -> int:
            return sum(
                1 for kw in agent["keywords"] or []
                if re.search(r"(?<![a-z0-9])" + re.escape(str(kw).lower()) + r"(?![a-z0-9])", text)
            )

        def least_busy(candidates: list[dict]) -> int:
            return min(candidates, key=lambda a: (a["load"], a["id"]))["id"]

        scores = {a["id"]: hits(a) for a in agents}
        matched = [a for a in agents if scores[a["id"]] > 0 and not a["coordinator"]]
        profiles = {a["profile_id"] for a in matched}
        if len(profiles) == 1:
            return least_busy(matched)
        coordinators = [a for a in agents if a["coordinator"]]
        if coordinators:
            return least_busy(coordinators)
        if matched:
            top = max(scores[a["id"]] for a in matched)
            return least_busy([a for a in matched if scores[a["id"]] == top])
        return agents[0]["id"]

    def create_task(
        self,
        title: str,
        description: str = "",
        agent_id: int | None = None,
        source: str = "api",
        source_ref: dict | None = None,
        requester: str = "",
        priority: int = 3,
        parent_id: int | None = None,
        thread_key: str = "",
        inline: bool = False,
    ) -> dict:
        title = (title or "").strip() or (description.strip().splitlines()[0][:120] if description.strip() else "Untitled task")
        ref = dict(source_ref or {})
        depth = 0
        if parent_id:
            parent = self.db.get("tasks", parent_id)
            depth = (parent["depth"] if parent else 0) + 1
        routed = False
        if agent_id is None:
            agent_id = self.route(title, description)
            routed = agent_id is not None
        values: dict[str, Any] = {
            "title": title[:300], "description": description, "agent_id": agent_id, "source": source,
            "source_ref": ref, "requester": requester[:200], "priority": priority if priority in PRIORITIES else 3,
            "parent_id": parent_id, "depth": depth, "thread_key": thread_key, "created_at": now(),
        }
        if inline:
            ref["inline"] = True
            values.update(status="running", started_at=now(), source_ref=ref)
        task_id = self.db.insert("tasks", values)
        who = self._agent_name(agent_id)
        self.add_event(task_id, "status", f"Created via {source}" + (f" by {requester}" if requester else "")
                       + (f"; routed to {who}" if routed else f"; assigned to {who}" if agent_id else "; waiting for an agent"))
        self.rt.bus.publish("task.updated", task_id=task_id)
        if not inline:
            self.kick()
        return self.db.get("tasks", task_id)

    def cancel(self, task_id: int) -> bool:
        task = self.db.get("tasks", task_id)
        if not task or task["status"] in TERMINAL:
            return False
        runner = self._running.get(task_id)
        if runner:
            runner.cancel()  # _execute records the cancellation
        else:
            self.db.update("tasks", task_id, {"status": "cancelled", "completed_at": now()})
            self.add_event(task_id, "status", "Cancelled")
            self.rt.bus.publish("task.updated", task_id=task_id)
        return True

    def retry(self, task_id: int) -> dict | None:
        task = self.db.get("tasks", task_id)
        if not task or task["status"] not in TERMINAL:
            return None
        self.db.update("tasks", task_id, {
            "status": "queued", "result": "", "error": "", "started_at": None, "completed_at": None,
            "confidence": None, "needs_review": 0, "escalations": 0, "model_id": "",
        })
        self.add_event(task_id, "status", "Re-queued for another run")
        self.rt.bus.publish("task.updated", task_id=task_id)
        self.kick()
        return self.db.get("tasks", task_id)

    async def wait_for(self, task_id: int, timeout: float) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            task = self.db.get("tasks", task_id)
            if task is None or task["status"] in TERMINAL or asyncio.get_running_loop().time() >= deadline:
                return task
            await asyncio.sleep(0.25)

    async def run_inline(self, task_id: int) -> dict:
        """Run a task inside the caller's coroutine (used when one agent consults another)."""
        await self._execute(task_id)
        return self.db.get("tasks", task_id)

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------
    def _task_message(self, task: dict) -> str:
        title, description = task["title"].strip(), task["description"].strip()
        lines = [f"# Task #{task['id']}: {title}"]
        if description and description != title:
            lines += ["", description]
        meta = []
        if task["requester"]:
            meta.append(f"Requested by: {task['requester']}")
        meta.append(f"Channel: {task['source']}")
        meta.append(f"Priority: {PRIORITIES.get(task['priority'], 'normal')}")
        meta.append(f"Date: {datetime.now().strftime('%Y-%m-%d')}")
        lines += ["", " | ".join(meta)]
        if task["thread_key"]:
            history = self.db.query(
                "SELECT title, description, result FROM tasks WHERE thread_key = ? AND id < ? AND status = 'completed' "
                "ORDER BY id DESC LIMIT 5", [task["thread_key"], task["id"]],
            )
            if history:
                lines += ["", "## Earlier in this conversation (oldest first)"]
                for item in reversed(history):
                    asked = (item["description"] or item["title"]).strip()
                    lines += [f"\n**Requester:** {asked[:1500]}", f"\n**You answered:** {item['result'][:3000]}"]
        return "\n".join(lines)

    def resolve_ladder(self, agent: dict, profile: dict) -> dict | None:
        for ladder_id in (agent.get("ladder_id"), profile.get("ladder_id"), self.db.get_setting("default_ladder")):
            if ladder_id:
                ladder = self.db.get("ladders", ladder_id)
                if ladder:
                    return ladder
        return self.db.one("SELECT * FROM ladders ORDER BY id LIMIT 1")

    async def _execute(self, task_id: int) -> None:
        task = self.db.get("tasks", task_id)
        if not task:
            return
        self.rt.bus.publish("task.updated", task_id=task_id)

        def emit(kind: str, summary: str, data: dict | None = None) -> None:
            self.add_event(task_id, kind, summary, data)

        update: dict[str, Any] = {}
        try:
            agent = self.db.get("agents", task["agent_id"]) if task["agent_id"] else None
            if not agent:
                raise LadderError("The task has no agent assigned.")
            profile = self.db.get("profiles", agent["profile_id"])
            if not profile:
                raise LadderError(f"Agent '{agent['name']}' refers to a missing profile '{agent['profile_id']}'.")
            ladder = self.resolve_ladder(agent, profile)
            if not ladder:
                raise LadderError("No escalation ladder is configured.")
            tools = tools_for(profile.get("skills") or [])
            ctx = ToolContext(rt=self.rt, agent=agent, profile=profile, task=task)
            emit("status", f"{agent['name']} started work (ladder: {ladder['name']})")
            result = await run_ladder(
                self.rt, ladder,
                lambda final, tools_enabled: build_system_prompt(agent, profile, tools, final, tools_enabled),
                self._task_message(task), tools, ctx, emit,
                max_steps=int(self.db.get_setting("max_steps", 12)),
            )
            update = {
                "status": "completed", "result": result.text, "confidence": result.confidence,
                "needs_review": result.needs_review, "model_id": result.model_id, "escalations": result.escalations,
                "triage": result.triage, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cost": round(result.cost, 6),
            }
            flag = " - flagged for human review (low confidence)" if result.needs_review else ""
            emit("status", f"Completed by {agent['name']} using {result.model_name}"
                           + (f" after {result.escalations} escalation(s)" if result.escalations else "") + flag,
                 {"model": result.model_id, "confidence": result.confidence, "trail": result.trail})
        except asyncio.CancelledError:
            # On shutdown the task stays 'running' so _recover() puts it back on the queue.
            if not self._stopping:
                self.db.update("tasks", task_id, {"status": "cancelled", "completed_at": now()})
                emit("status", "Cancelled")
                self.rt.bus.publish("task.updated", task_id=task_id)
            raise
        except LadderError as exc:
            update = {"status": "failed", "error": str(exc)}
            emit("status", f"Failed: {exc}")
        except Exception as exc:
            log.exception("task %s crashed", task_id)
            update = {"status": "failed", "error": f"Internal error: {exc.__class__.__name__}: {exc}"}
            emit("status", f"Failed: internal error ({exc.__class__.__name__})")

        update["completed_at"] = now()
        self.db.update("tasks", task_id, update)
        self.rt.bus.publish("task.updated", task_id=task_id)
        try:
            await self.rt.channels.task_finished(self.db.get("tasks", task_id))
        except Exception:
            log.exception("completion notification for task %s failed", task_id)
