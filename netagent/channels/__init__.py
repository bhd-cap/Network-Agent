"""Channel manager: configuration storage, result delivery and notifications."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import httpx

from . import email as email_channel
from . import teams as teams_channel
from .common import format_result

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

log = logging.getLogger("netagent.channels")

NOTIFY_SCHEMA = [
    {"key": "teams_webhook_url", "label": "Teams channel webhook URL", "type": "secret",
     "help": "Incoming-webhook / Workflows URL of the channel that should receive task cards."},
    {"key": "email_recipients", "label": "Email recipients", "type": "text",
     "help": "Comma separated. Uses the mailbox configured under Email."},
    {"key": "on_completed", "label": "Notify when a task completes", "type": "bool"},
    {"key": "on_failed", "label": "Notify when a task fails", "type": "bool"},
    {"key": "on_approval", "label": "Notify when a change request needs approval", "type": "bool"},
    {"key": "include_result", "label": "Include the result text in notifications", "type": "bool"},
    {"key": "daily_digest", "label": "Send a daily summary report", "type": "bool"},
    {"key": "digest_hour", "label": "Daily report hour (0-23, server time)", "type": "number"},
]
NOTIFY_DEFAULTS = {"on_completed": True, "on_failed": True, "on_approval": True, "include_result": True,
                   "daily_digest": False, "digest_hour": 8}

CHANNELS = {
    "teams": {"title": "Microsoft Teams", "schema": teams_channel.SCHEMA, "defaults": teams_channel.DEFAULTS},
    "email": {"title": "Email", "schema": email_channel.SCHEMA, "defaults": email_channel.DEFAULTS},
    "notifications": {"title": "Notifications & reports", "schema": NOTIFY_SCHEMA, "defaults": NOTIFY_DEFAULTS},
}
_ENC = "enc:"


def adaptive_card(title: str, facts: list[tuple[str, str]], body: str, url: str, colour: str = "default") -> dict:
    """Teams message carrying an Adaptive Card (accepted by incoming webhooks and Workflows)."""
    blocks: list[dict] = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True, "color": colour},
        {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts if v]},
    ]
    if body:
        blocks.append({"type": "TextBlock", "text": body, "wrap": True, "spacing": "Medium"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
                "body": blocks, "actions": [{"type": "Action.OpenUrl", "title": "Open in console", "url": url}],
            },
        }],
    }


class ChannelManager:
    def __init__(self, rt: "Runtime"):
        self.rt = rt
        self.teams = teams_channel.TeamsChannel(rt)
        self.email = email_channel.EmailChannel(rt)
        self._tasks: list[asyncio.Task] = []
        self.last_notify_error = ""

    # ------------------------------------------------------------------
    # configuration (secrets are encrypted at rest and never returned)
    # ------------------------------------------------------------------
    def get_config(self, name: str) -> dict:
        spec = CHANNELS[name]
        stored = self.rt.db.get_setting(f"channel.{name}", {}) or {}
        cfg = dict(spec["defaults"])
        for key, value in stored.items():
            if isinstance(value, str) and value.startswith(_ENC):
                value = self.rt.box.decrypt(value[len(_ENC):])
            cfg[key] = value
        return cfg

    def public_config(self, name: str) -> dict:
        cfg = self.get_config(name)
        for field in CHANNELS[name]["schema"]:
            if field["type"] == "secret":
                cfg[field["key"]] = "********" if cfg.get(field["key"]) else ""
        return cfg

    def set_config(self, name: str, values: dict) -> None:
        spec = CHANNELS[name]
        stored = self.rt.db.get_setting(f"channel.{name}", {}) or {}
        for field in spec["schema"]:
            key = field["key"]
            if key not in values:
                continue
            value = values[key]
            if field["type"] == "secret":
                if value in (None, "********"):
                    continue  # unchanged
                stored[key] = _ENC + self.rt.box.encrypt(str(value)) if value else ""
            elif field["type"] == "bool":
                stored[key] = bool(value)
            elif field["type"] == "number":
                try:
                    stored[key] = int(value)
                except (TypeError, ValueError):
                    stored[key] = spec["defaults"].get(key, 0)
            else:
                stored[key] = str(value or "").strip()
        self.rt.db.set_setting(f"channel.{name}", stored)

    def status(self) -> dict:
        teams_cfg, email_cfg, note_cfg = (self.get_config(n) for n in ("teams", "email", "notifications"))
        base = self.rt.settings.public_url
        return {
            "teams": {
                "enabled": bool(teams_cfg.get("enabled")), "error": self.teams.last_error,
                "endpoints": {"bot": f"{base}/hooks/teams/messages", "outgoing_webhook": f"{base}/hooks/teams/outgoing"},
            },
            "email": {
                "enabled": bool(email_cfg.get("enabled")), "error": self.email.last_error,
                "last_poll": self.email.last_poll, "ignored": self.email.ignored,
            },
            "notifications": {
                "enabled": bool(note_cfg.get("teams_webhook_url") or note_cfg.get("email_recipients")),
                "error": self.last_notify_error,
            },
        }

    # ------------------------------------------------------------------
    # background loops
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self.email.run(), name="netagent-email"),
            asyncio.create_task(self._digest_loop(), name="netagent-digest"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _digest_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                cfg = self.get_config("notifications")
                if not cfg.get("daily_digest"):
                    continue
                today = time.strftime("%Y-%m-%d")
                if time.localtime().tm_hour >= int(cfg.get("digest_hour") or 8) and \
                        self.rt.db.get_setting("digest.last_sent") != today:
                    self.rt.db.set_setting("digest.last_sent", today)
                    await self.send_digest(1)
            except Exception:
                log.exception("digest loop error")

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------
    async def _post_teams_webhook(self, url: str, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 300:
            raise RuntimeError(f"Teams webhook returned HTTP {resp.status_code}: {resp.text[:200]}")

    async def broadcast(self, title: str, facts: list[tuple[str, str]], body: str, url: str, colour: str = "default") -> list[str]:
        """Send to the notification targets. Returns a list of problems (empty when all went out)."""
        cfg = self.get_config("notifications")
        problems: list[str] = []
        if cfg.get("teams_webhook_url"):
            try:
                await self._post_teams_webhook(cfg["teams_webhook_url"], adaptive_card(title, facts, body[:3500], url, colour))
            except Exception as exc:
                problems.append(f"Teams webhook: {exc}")
        recipients = [r.strip() for r in str(cfg.get("email_recipients") or "").split(",") if r.strip()]
        if recipients:
            if not self.get_config("email").get("mailbox"):
                problems.append("Email: no mailbox configured under Email")
            else:
                text = "\n".join([title, "", *[f"{k}: {v}" for k, v in facts if v], "", body, "", url])
                try:
                    await self.email.send(recipients, f"[BlackHawk NetAgent] {title}", text)
                except Exception as exc:
                    problems.append(f"Email: {exc}")
        self.last_notify_error = "; ".join(problems)
        for problem in problems:
            log.warning("notification failed: %s", problem)
        return problems

    async def task_finished(self, task: dict) -> None:
        """Deliver a finished task to its requester and to the notification targets."""
        if task["status"] not in {"completed", "failed"} or task["source"] == "agent":
            return  # sub-tasks report to their parent agent, not to people
        ref = task["source_ref"] or {}
        delivered = False
        if task["source"] == "teams":
            delivered = await self.teams.deliver_result(task) or bool(ref.get("answered_inline"))
        elif task["source"] == "email":
            delivered = await self.email.deliver_result(task)
        if ref.get("callback_url"):
            delivered = await self._callback(task, ref["callback_url"]) or delivered
        if delivered:
            self.rt.orchestrator.add_event(task["id"], "delivery", f"Result delivered to the requester via {task['source']}")

        cfg = self.get_config("notifications")
        wanted = cfg.get("on_completed") if task["status"] == "completed" else cfg.get("on_failed")
        if not wanted or not (cfg.get("teams_webhook_url") or cfg.get("email_recipients")):
            return
        agent = self.rt.db.get("agents", task["agent_id"]) if task["agent_id"] else None
        model = self.rt.db.get("models", task["model_id"]) if task["model_id"] else None
        ok = task["status"] == "completed"
        title = f"{'Completed' if ok else 'FAILED'}: #{task['id']} {task['title']}"
        if ok and task["needs_review"]:
            title += " (needs human review)"
        body = ""
        if ok and cfg.get("include_result"):
            body = task["result"]
        elif not ok:
            body = task["error"]
        await self.broadcast(
            title,
            [("Agent", agent["name"] if agent else ""), ("Requested by", task["requester"]), ("Channel", task["source"]),
             ("Model", model["name"] if model else ""),
             ("Escalations", str(task["escalations"]) if task["escalations"] else "")],
            body, f"{self.rt.settings.public_url}/#/tasks/{task['id']}",
            "good" if ok and not task["needs_review"] else "warning" if ok else "attention",
        )

    async def change_requested(self, change: dict) -> None:
        cfg = self.get_config("notifications")
        if not cfg.get("on_approval"):
            return
        device = self.rt.db.get("devices", change["device_id"])
        agent = self.rt.db.get("agents", change["agent_id"]) if change["agent_id"] else None
        await self.broadcast(
            f"Approval needed: CR-{change['id']} {change['summary']}",
            [("Device", device["name"] if device else ""), ("Proposed by", agent["name"] if agent else ""),
             ("Risk", change["risk"]), ("Lines", str(len(change["commands"].splitlines())))],
            change["rationale"], f"{self.rt.settings.public_url}/#/approvals", "warning",
        )

    async def _callback(self, task: dict, url: str) -> bool:
        payload = {
            "task_id": task["id"], "status": task["status"], "title": task["title"], "result": task["result"],
            "error": task["error"], "needs_review": bool(task["needs_review"]), "confidence": task["confidence"],
            "model": task["model_id"], "escalations": task["escalations"], "completed_at": task["completed_at"],
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code >= 300:
                raise RuntimeError(f"HTTP {resp.status_code}")
            return True
        except Exception as exc:
            self.rt.orchestrator.add_event(task["id"], "delivery", f"Callback to {url} failed: {exc}")
            return False

    async def send_digest(self, days: int = 1) -> list[str]:
        from ..reports import digest_markdown

        text = digest_markdown(self.rt, days)
        title, _, body = text.partition("\n")
        return await self.broadcast(title.lstrip("# "), [], body.strip(), self.rt.settings.public_url)

    async def test(self, name: str) -> tuple[bool, str]:
        if name == "teams":
            return await self.teams.test()
        if name == "email":
            return await self.email.test()
        problems = await self.broadcast("Test notification", [("Source", "BlackHawk NetAgent console")],
                                        "If you can read this, notifications are working.", self.rt.settings.public_url)
        cfg = self.get_config("notifications")
        if not (cfg.get("teams_webhook_url") or cfg.get("email_recipients")):
            return False, "configure a Teams webhook URL or email recipients first"
        return (not problems), "; ".join(problems) or "test notification sent"


__all__ = ["ChannelManager", "CHANNELS", "format_result"]
