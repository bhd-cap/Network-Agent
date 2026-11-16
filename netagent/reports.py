"""Task reporting: summaries, per-task reports and CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    try:
        return max(0.0, (datetime.strptime(end, fmt) - datetime.strptime(start, fmt)).total_seconds())
    except ValueError:
        return None


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def summary(rt: "Runtime", days: int = 7) -> dict:
    since = _since(days)
    db = rt.db
    tasks = db.query(
        "SELECT t.*, a.name AS agent_name, m.name AS model_name, m.location AS model_location FROM tasks t "
        "LEFT JOIN agents a ON a.id = t.agent_id LEFT JOIN models m ON m.id = t.model_id "
        "WHERE t.created_at >= ? ORDER BY t.id DESC", [since],
    )
    top = [t for t in tasks if t["parent_id"] is None]
    done = [t for t in tasks if t["status"] == "completed"]
    durations = [d for t in done if (d := _seconds(t["started_at"], t["completed_at"])) is not None]
    finished = [t for t in tasks if t["status"] in {"completed", "failed"}]

    def bucket(rows: list[dict], key: str, default: str = "-") -> list[dict]:
        groups: dict[str, dict] = {}
        for t in rows:
            name = t.get(key) or default
            g = groups.setdefault(name, {"name": name, "total": 0, "completed": 0, "failed": 0, "open": 0,
                                         "escalated": 0, "cost": 0.0, "_dur": []})
            g["total"] += 1
            if t["status"] == "completed":
                g["completed"] += 1
                d = _seconds(t["started_at"], t["completed_at"])
                if d is not None:
                    g["_dur"].append(d)
            elif t["status"] == "failed":
                g["failed"] += 1
            elif t["status"] in {"queued", "running"}:
                g["open"] += 1
            g["escalated"] += 1 if t["escalations"] else 0
            g["cost"] += t["cost"] or 0
        out = []
        for g in groups.values():
            dur = g.pop("_dur")
            g["avg_seconds"] = round(sum(dur) / len(dur), 1) if dur else None
            g["cost"] = round(g["cost"], 4)
            out.append(g)
        return sorted(out, key=lambda g: -g["total"])

    by_day: dict[str, dict] = {}
    for t in tasks:
        day = t["created_at"][:10]
        d = by_day.setdefault(day, {"day": day, "created": 0, "completed": 0, "failed": 0})
        d["created"] += 1
        if t["status"] == "completed":
            d["completed"] += 1
        elif t["status"] == "failed":
            d["failed"] += 1

    return {
        "days": days,
        "since": since,
        "totals": {
            "tasks": len(tasks),
            "top_level": len(top),
            "sub_tasks": len(tasks) - len(top),
            "completed": len(done),
            "failed": sum(1 for t in tasks if t["status"] == "failed"),
            "cancelled": sum(1 for t in tasks if t["status"] == "cancelled"),
            "open": sum(1 for t in tasks if t["status"] in {"queued", "running"}),
            "needs_review": sum(1 for t in done if t["needs_review"]),
            "escalated": sum(1 for t in done if t["escalations"]),
            "success_rate": round(100 * len(done) / len(finished), 1) if finished else None,
            "escalation_rate": round(100 * sum(1 for t in done if t["escalations"]) / len(done), 1) if done else None,
            "avg_seconds": round(sum(durations) / len(durations), 1) if durations else None,
            "local_share": round(100 * sum(1 for t in done if t["model_location"] == "local") / len(done), 1) if done else None,
            "input_tokens": sum(t["input_tokens"] or 0 for t in tasks),
            "output_tokens": sum(t["output_tokens"] or 0 for t in tasks),
            "cost": round(sum(t["cost"] or 0 for t in tasks), 4),
        },
        "by_agent": bucket(tasks, "agent_name", "unassigned"),
        "by_model": bucket(done, "model_name"),
        "by_source": bucket(top, "source"),
        "by_day": sorted(by_day.values(), key=lambda d: d["day"]),
        "recent": [
            {"id": t["id"], "title": t["title"], "status": t["status"], "agent": t["agent_name"], "model": t["model_name"],
             "source": t["source"], "requester": t["requester"], "escalations": t["escalations"],
             "needs_review": t["needs_review"], "created_at": t["created_at"], "completed_at": t["completed_at"],
             "seconds": _seconds(t["started_at"], t["completed_at"])}
            for t in top[:50]
        ],
    }


def digest_markdown(rt: "Runtime", days: int = 1) -> str:
    s = summary(rt, days)
    t = s["totals"]
    period = "last 24 hours" if days == 1 else f"last {days} days"
    lines = [
        f"# NetAgent report - {period}", "",
        f"- Tasks: **{t['tasks']}** ({t['top_level']} requests, {t['sub_tasks']} agent sub-tasks)",
        f"- Completed: **{t['completed']}**, failed: **{t['failed']}**, open: **{t['open']}**",
        f"- Flagged for human review: **{t['needs_review']}**",
        f"- Escalated to a larger model: **{t['escalated']}**"
        + (f" ({t['escalation_rate']}% of completed)" if t["escalation_rate"] is not None else ""),
        f"- Average time to complete: **{fmt_duration(t['avg_seconds'])}**",
        f"- Estimated model cost: **${t['cost']:.2f}**",
    ]
    if s["by_agent"]:
        lines += ["", "## By agent"]
        lines += [f"- {a['name']}: {a['completed']} completed, {a['failed']} failed, {a['open']} open" for a in s["by_agent"]]
    if s["by_model"]:
        lines += ["", "## Completed by model"]
        lines += [f"- {m['name']}: {m['completed']}" for m in s["by_model"]]
    open_rows = rt.db.query(
        "SELECT t.id, t.title, t.status, a.name AS agent FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id "
        "WHERE t.status IN ('queued','running') AND t.parent_id IS NULL ORDER BY t.priority, t.id LIMIT 20"
    )
    if open_rows:
        lines += ["", "## Open"]
        lines += [f"- #{r['id']} [{r['status']}] {r['title']} - {r['agent'] or 'unassigned'}" for r in open_rows]
    pending = rt.db.scalar("SELECT COUNT(*) FROM change_requests WHERE status = 'pending'")
    if pending:
        lines += ["", f"**{pending} change request(s) are waiting for approval.**"]
    lines += ["", f"Console: {rt.settings.public_url}"]
    return "\n".join(lines)


def task_report(rt: "Runtime", task_id: int) -> str | None:
    db = rt.db
    task = db.get("tasks", task_id)
    if not task:
        return None
    agent = db.get("agents", task["agent_id"]) if task["agent_id"] else None
    model = db.get("models", task["model_id"]) if task["model_id"] else None
    lines = [
        f"# Task #{task['id']}: {task['title']}", "",
        f"- Status: **{task['status']}**" + (" (flagged for human review)" if task["needs_review"] else ""),
        f"- Agent: {agent['name'] if agent else 'unassigned'}",
        f"- Requested by: {task['requester'] or '-'} via {task['source']}",
        f"- Created: {task['created_at']}",
        f"- Completed: {task['completed_at'] or '-'} (run time {fmt_duration(_seconds(task['started_at'], task['completed_at']))})",
        f"- Answered by: {model['name'] if model else '-'}"
        + (f" after {task['escalations']} escalation(s)" if task["escalations"] else ""),
        f"- Confidence: {task['confidence'] if task['confidence'] is not None else '-'}",
        f"- Tokens: {task['input_tokens']} in / {task['output_tokens']} out, est. cost ${task['cost']:.4f}",
    ]
    if task["description"] and task["description"] != task["title"]:
        lines += ["", "## Request", "", task["description"]]
    if task["result"]:
        lines += ["", "## Result", "", task["result"]]
    if task["error"]:
        lines += ["", "## Error", "", task["error"]]
    children = db.query(
        "SELECT t.id, t.title, t.status, a.name AS agent FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id "
        "WHERE t.parent_id = ? ORDER BY t.id", [task_id],
    )
    if children:
        lines += ["", "## Sub-tasks"]
        lines += [f"- #{c['id']} [{c['status']}] {c['title']} - {c['agent'] or 'unassigned'}" for c in children]
    changes = db.query(
        "SELECT c.*, d.name AS device FROM change_requests c LEFT JOIN devices d ON d.id = c.device_id WHERE c.task_id = ?",
        [task_id],
    )
    if changes:
        lines += ["", "## Change requests"]
        lines += [f"- CR-{c['id']} [{c['status']}] {c['device']}: {c['summary']}" for c in changes]
    events = db.query("SELECT ts, kind, summary FROM task_events WHERE task_id = ? ORDER BY id", [task_id])
    if events:
        lines += ["", "## Timeline"]
        lines += [f"- {e['ts'][11:19]} [{e['kind']}] {e['summary']}" for e in events]
    return "\n".join(lines)


def _cell(value) -> str:
    """Neutralise spreadsheet formulas in free-text cells (CSV injection)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def tasks_csv(rt: "Runtime", days: int = 30) -> str:
    rows = rt.db.query(
        "SELECT t.*, a.name AS agent_name, m.name AS model_name FROM tasks t LEFT JOIN agents a ON a.id = t.agent_id "
        "LEFT JOIN models m ON m.id = t.model_id WHERE t.created_at >= ? ORDER BY t.id", [_since(days)],
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "parent_id", "title", "status", "priority", "agent", "source", "requester", "model",
                     "escalations", "confidence", "needs_review", "created_at", "started_at", "completed_at",
                     "run_seconds", "input_tokens", "output_tokens", "cost_usd", "error"])
    for t in rows:
        seconds = _seconds(t["started_at"], t["completed_at"])
        writer.writerow([
            t["id"], t["parent_id"] or "", _cell(t["title"]), t["status"], t["priority"], _cell(t["agent_name"]),
            t["source"], _cell(t["requester"]), _cell(t["model_name"]), t["escalations"], "" if t["confidence"] is None else t["confidence"],
            int(bool(t["needs_review"])), t["created_at"], t["started_at"] or "", t["completed_at"] or "",
            "" if seconds is None else round(seconds, 1), t["input_tokens"], t["output_tokens"], t["cost"], _cell(t["error"]),
        ])
    return buf.getvalue()
