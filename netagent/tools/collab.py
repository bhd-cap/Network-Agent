"""Agent-to-agent collaboration tools."""
from __future__ import annotations

import asyncio

from . import ToolContext, ToolError, tool

TERMINAL = {"completed", "failed", "cancelled"}


def _resolve_agent(ctx: ToolContext, name: str) -> dict:
    name = str(name).strip()
    agent = ctx.rt.db.one("SELECT * FROM agents WHERE name = ? COLLATE NOCASE", [name])
    if not agent and name.isdigit():
        agent = ctx.rt.db.get("agents", int(name))
    if not agent:
        names = ", ".join(a["name"] for a in ctx.rt.db.query("SELECT name FROM agents ORDER BY name"))
        raise ToolError(f"No agent named '{name}'. Agents: {names or 'none'}.")
    if agent["id"] == ctx.agent["id"]:
        raise ToolError("You cannot consult or delegate to yourself.")
    if agent["status"] != "active":
        raise ToolError(f"Agent '{agent['name']}' is paused and cannot take work right now.")
    return agent


def _check_depth(ctx: ToolContext) -> None:
    limit = int(ctx.rt.db.get_setting("max_collab_depth", 3))
    if int(ctx.task.get("depth") or 0) >= limit:
        raise ToolError(
            f"Collaboration depth limit ({limit}) reached - this task is already a sub-task of a sub-task. "
            "Answer with what you know and state what remains open."
        )


@tool(
    "list_agents", "collaboration",
    "List the other agents on this platform with their specialities and current workload. Use it to decide who "
    "to consult or delegate to.",
)
async def list_agents(ctx: ToolContext, args: dict) -> str:
    rows = ctx.rt.db.query(
        "SELECT a.*, p.name AS profile_name, p.description AS profile_description, p.keywords AS keywords, "
        "p.coordinator AS coordinator FROM agents a JOIN profiles p ON p.id = a.profile_id ORDER BY a.name"
    )
    lines = []
    for a in rows:
        if a["id"] == ctx.agent["id"]:
            continue
        open_tasks = ctx.rt.db.scalar(
            "SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status IN ('queued','running')", [a["id"]]
        )
        role = "coordinator" if a["coordinator"] else "specialist"
        keywords = ", ".join((a["keywords"] or [])[:8])
        lines.append(
            f"- **{a['name']}** ({a['profile_name']}, {role}, {a['status']}, {open_tasks} open task(s)): "
            f"{a['profile_description']}" + (f" Topics: {keywords}." if keywords else "")
        )
    return "\n".join(lines) if lines else "There are no other agents. Handle the task yourself."


@tool(
    "consult_agent", "collaboration",
    "Ask another agent a question and wait for its answer. Use this when part of the problem sits in another "
    "agent's speciality. Give it everything it needs - it does not see your conversation. The exchange is "
    "recorded as a sub-task.",
    {
        "agent": {"type": "string", "description": "Agent name (see list_agents)"},
        "question": {"type": "string", "description": "A self-contained question, including relevant config, output and constraints"},
    },
    ["agent", "question"],
)
async def consult_agent(ctx: ToolContext, args: dict) -> str:
    _check_depth(ctx)
    target = _resolve_agent(ctx, args["agent"])
    question = str(args["question"]).strip()
    orch = ctx.rt.orchestrator
    child = orch.create_task(
        title=f"Consult from {ctx.agent['name']}: {question.splitlines()[0][:90]}",
        description=question, agent_id=target["id"], source="agent", parent_id=ctx.task["id"],
        requester=ctx.agent["name"], priority=ctx.task.get("priority", 3), inline=True,
    )
    orch.add_event(ctx.task["id"], "collab", f"Consulting {target['name']} (task #{child['id']})",
                   {"child_task_id": child["id"], "agent": target["name"]})
    done = await orch.run_inline(child["id"])
    if done["status"] != "completed":
        raise ToolError(f"{target['name']} could not answer (task #{child['id']} {done['status']}): {done['error'] or 'no detail'}")
    flag = " (the agent flagged low confidence - verify before relying on it)" if done["needs_review"] else ""
    return f"Answer from {target['name']} (task #{child['id']}){flag}:\n\n{done['result']}"


@tool(
    "delegate_task", "collaboration",
    "Hand a self-contained piece of work to another agent to run in the background. Returns a task ID "
    "immediately; collect the result later with check_task. Prefer consult_agent when you need the answer to "
    "continue.",
    {
        "agent": {"type": "string", "description": "Agent name (see list_agents)"},
        "title": {"type": "string", "description": "Short task title"},
        "instructions": {"type": "string", "description": "Complete, self-contained instructions and context"},
    },
    ["agent", "title", "instructions"],
)
async def delegate_task(ctx: ToolContext, args: dict) -> str:
    _check_depth(ctx)
    target = _resolve_agent(ctx, args["agent"])
    orch = ctx.rt.orchestrator
    child = orch.create_task(
        title=str(args["title"])[:200], description=str(args["instructions"]), agent_id=target["id"],
        source="agent", parent_id=ctx.task["id"], requester=ctx.agent["name"],
        priority=ctx.task.get("priority", 3),
    )
    orch.add_event(ctx.task["id"], "collab", f"Delegated task #{child['id']} to {target['name']}: {child['title']}",
                   {"child_task_id": child["id"], "agent": target["name"]})
    return f"Task #{child['id']} queued for {target['name']}. Use check_task with task_id={child['id']} to collect the result."


@tool(
    "check_task", "collaboration",
    "Get the status and result of a task you delegated. Optionally wait for it to finish.",
    {
        "task_id": {"type": "integer", "description": "ID returned by delegate_task"},
        "wait_seconds": {"type": "integer", "description": "Wait up to this many seconds for completion (max 300)"},
    },
    ["task_id"],
)
async def check_task(ctx: ToolContext, args: dict) -> str:
    try:
        task_id = int(args["task_id"])
    except (TypeError, ValueError) as exc:
        raise ToolError("task_id must be an integer.") from exc
    wait = max(0, min(int(args.get("wait_seconds") or 0), 300))
    task = ctx.rt.db.get("tasks", task_id)
    if not task or task["parent_id"] != ctx.task["id"]:
        raise ToolError(f"Task #{task_id} is not a sub-task of your current task.")
    waited = 0.0
    while task["status"] not in TERMINAL and waited < wait:
        await asyncio.sleep(1.0)
        waited += 1.0
        task = ctx.rt.db.get("tasks", task_id)
    if task["status"] == "completed":
        return f"Task #{task_id} completed:\n\n{task['result']}"
    if task["status"] in TERMINAL:
        return f"Task #{task_id} {task['status']}: {task['error'] or 'no detail'}"
    return f"Task #{task_id} is still {task['status']}. Check again later or continue with other work."
