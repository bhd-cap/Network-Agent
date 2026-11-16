"""Helpers shared by the chat-style channels (Teams, email)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

HELP = (
    "**NetAgent** - send me a question or a task and an agent will pick it up.\n\n"
    "- `agents` - list the agents and what they do\n"
    "- `tasks` - show open tasks\n"
    "- `status <id>` - status and result of a task\n"
    "- `<agent name>: <request>` - address a specific agent (otherwise the request is routed automatically)"
)


def pick_agent(rt: "Runtime", text: str, default_agent: str = "") -> tuple[int | None, str]:
    """Detect '<agent>: ...', '@agent ...' or '[agent] ...' prefixes. Returns (agent_id or None, remaining text)."""
    stripped = text.strip()
    low = stripped.lower()
    agents = sorted(rt.db.query("SELECT id, name FROM agents WHERE status = 'active'"), key=lambda a: -len(a["name"]))
    for agent in agents:
        name = agent["name"].lower()
        for prefix in (f"{name}:", f"@{name}", f"[{name}]"):
            if low.startswith(prefix):
                rest = stripped[len(prefix):].lstrip(" :,-")
                return agent["id"], rest
    if default_agent:
        row = rt.db.one("SELECT id FROM agents WHERE name = ? COLLATE NOCASE AND status = 'active'", [default_agent])
        if row:
            return row["id"], stripped
    return None, stripped


def handle_command(rt: "Runtime", text: str) -> str | None:
    """Built-in chat commands. Returns the reply, or None when the text is a normal request."""
    low = text.strip().lower()
    if low in {"help", "?", "hi", "hello"}:
        return HELP
    if low in {"agents", "list agents"}:
        rows = rt.db.query(
            "SELECT a.name, a.status, p.name AS profile, p.description FROM agents a JOIN profiles p ON p.id = a.profile_id ORDER BY a.name"
        )
        if not rows:
            return "No agents are configured yet."
        return "\n".join(f"- **{r['name']}** ({r['profile']}, {r['status']}) - {r['description']}" for r in rows)
    if low in {"tasks", "open tasks", "queue"}:
        rows = rt.db.query(
            "SELECT t.id, t.title, t.status, a.name AS agent FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id "
            "WHERE t.status IN ('queued','running') AND t.parent_id IS NULL ORDER BY t.priority, t.id LIMIT 15"
        )
        if not rows:
            return "No open tasks."
        return "\n".join(f"- #{r['id']} [{r['status']}] {r['title']} - {r['agent'] or 'unassigned'}" for r in rows)
    match = re.fullmatch(r"(?:status|task)\s+#?(\d+)", low)
    if match:
        task = rt.db.get("tasks", int(match.group(1)))
        if not task:
            return f"There is no task #{match.group(1)}."
        return format_result(rt, task)
    return None


def format_result(rt: "Runtime", task: dict, limit: int = 12000) -> str:
    """Markdown summary of a task for chat/email delivery."""
    agent = rt.db.get("agents", task["agent_id"]) if task["agent_id"] else None
    who = agent["name"] if agent else "unassigned"
    link = f"{rt.settings.public_url}/#/tasks/{task['id']}"
    if task["status"] == "completed":
        body = task["result"]
        if len(body) > limit:
            body = body[:limit] + f"\n\n... (truncated - full result: {link})"
        model = rt.db.get("models", task["model_id"]) if task["model_id"] else None
        footer = f"Task #{task['id']} - {who}" + (f" - {model['name']}" if model else "")
        if task["needs_review"]:
            footer += " - LOW CONFIDENCE: please have an engineer review this"
        return f"{body}\n\n---\n{footer}"
    if task["status"] == "failed":
        return f"Task #{task['id']} ({task['title']}) failed: {task['error'] or 'unknown error'}\n\n{link}"
    if task["status"] == "cancelled":
        return f"Task #{task['id']} ({task['title']}) was cancelled."
    return f"Task #{task['id']} ({task['title']}) is **{task['status']}** with {who}. {link}"
