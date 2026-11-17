"""Task-tracking tools available to every agent."""
from __future__ import annotations

from . import ToolContext, tool


@tool(
    "add_task_note", "task-tracking",
    "Post a short progress note on the task timeline so people watching the console can follow along. Use it "
    "at meaningful milestones of longer tasks (what was found, what is next) - not for every step.",
    {"note": {"type": "string", "description": "One or two sentences"}},
    ["note"],
)
async def add_task_note(ctx: ToolContext, args: dict) -> str:
    ctx.rt.orchestrator.add_event(ctx.task["id"], "note", str(args["note"])[:1000])
    return "Noted."
