"""Email: tasks in by mail, results back as replies.

Two backends:

* ``graph``      - Microsoft 365 via Microsoft Graph (app registration with
                   Mail.ReadWrite + Mail.Send application permissions). Use this
                   for Exchange Online, where basic-auth IMAP/SMTP is disabled.
* ``imap_smtp``  - any standard mailbox.

Only senders on the allow-list create tasks; everything else is ignored.
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import html
import imaplib
import logging
import re
import smtplib
import time
from email.message import EmailMessage
from email.utils import parseaddr
from typing import TYPE_CHECKING

import httpx

from .common import format_result, pick_agent

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

log = logging.getLogger("netagent.email")
GRAPH = "https://graph.microsoft.com/v1.0"

SCHEMA = [
    {"key": "enabled", "label": "Enable email", "type": "bool"},
    {"key": "backend", "label": "Mail system", "type": "select", "options": ["graph", "imap_smtp"],
     "help": "graph = Microsoft 365 (recommended for Exchange Online). imap_smtp = any other mailbox."},
    {"key": "mailbox", "label": "Agent mailbox address", "type": "text",
     "help": "The address people write to; replies are sent from it."},
    {"key": "allowed_senders", "label": "Allowed senders", "type": "text",
     "help": "Comma separated addresses or @domains, e.g. noc@example.com, @example.com. Mail from anyone else is ignored."},
    {"key": "poll_seconds", "label": "Check for new mail every (seconds)", "type": "number"},
    {"key": "default_agent", "label": "Default agent", "type": "text",
     "help": "Agent name for mail that does not address an agent (subject prefix '[Agent name]'). Empty = automatic routing."},
    {"key": "tenant_id", "label": "Graph: Directory (tenant) ID", "type": "text"},
    {"key": "client_id", "label": "Graph: Application (client) ID", "type": "text"},
    {"key": "client_secret", "label": "Graph: client secret", "type": "secret"},
    {"key": "imap_host", "label": "IMAP: server", "type": "text"},
    {"key": "imap_port", "label": "IMAP: port", "type": "number"},
    {"key": "smtp_host", "label": "SMTP: server", "type": "text"},
    {"key": "smtp_port", "label": "SMTP: port", "type": "number"},
    {"key": "smtp_security", "label": "SMTP: security", "type": "select", "options": ["starttls", "ssl", "none"]},
    {"key": "username", "label": "IMAP/SMTP: username", "type": "text"},
    {"key": "password", "label": "IMAP/SMTP: password", "type": "secret"},
]
DEFAULTS = {"enabled": False, "backend": "graph", "poll_seconds": 60, "imap_port": 993, "smtp_port": 587,
            "smtp_security": "starttls"}

_QUOTE_MARKERS = [
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^On .{5,200} wrote:\s*$", re.I),
    re.compile(r"^_{10,}\s*$"),
]
_OUTLOOK_FROM = re.compile(r"^From:\s.+", re.I)
_OUTLOOK_NEXT = re.compile(r"^(Sent|Date|To):\s", re.I)


def sender_allowed(address: str, allowed: str) -> bool:
    address = address.strip().lower()
    if not address or "@" not in address:
        return False
    for entry in (e.strip().lower() for e in allowed.split(",")):
        if not entry:
            continue
        if entry.startswith("@"):
            if address.endswith(entry):
                return True
        elif address == entry:
            return True
    return False


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style).*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def strip_quoted(body: str) -> str:
    """Drop quoted history so only the new part of a reply becomes the task."""
    kept: list[str] = []
    lines = body.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        if any(marker.match(line.strip()) for marker in _QUOTE_MARKERS):
            break
        # Outlook quotes the previous mail as "From: ... / Sent: ..." header lines.
        if _OUTLOOK_FROM.match(line.strip()) and index + 1 < len(lines) and _OUTLOOK_NEXT.match(lines[index + 1].strip()):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def clean_subject(subject: str) -> str:
    return re.sub(r"^\s*((re|fw|fwd|aw|wg)\s*:\s*)+", "", subject or "", flags=re.I).strip()


def parse_rfc822(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    name, address = parseaddr(str(msg.get("From", "")))
    part = msg.get_body(preferencelist=("plain", "html"))
    body = ""
    if part is not None:
        try:
            body = part.get_content()
        except Exception:
            body = ""
        if part.get_content_type() == "text/html":
            body = html_to_text(body)
    auto = str(msg.get("Auto-Submitted", "no")).lower() != "no" or \
        str(msg.get("Precedence", "")).lower() in {"bulk", "junk", "auto_reply", "list"} or \
        bool(msg.get("X-Auto-Response-Suppress"))
    references = str(msg.get("References", "")).split()
    message_id = str(msg.get("Message-ID", "")).strip()
    return {
        "id": message_id, "from_addr": address.lower(), "from_name": name or address, "subject": str(msg.get("Subject", "")),
        "body": body, "message_id": message_id, "references": references, "auto": auto,
        "thread": references[0] if references else message_id,
    }


class EmailChannel:
    def __init__(self, rt: "Runtime"):
        self.rt = rt
        self._graph_token: tuple[float, str] | None = None
        self.last_error = ""
        self.last_poll = ""
        self.ignored = 0

    def config(self) -> dict:
        return self.rt.channels.get_config("email")

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    async def run(self) -> None:
        while True:
            cfg = self.config()
            if cfg.get("enabled"):
                try:
                    await self.poll_once(cfg)
                    self.last_error = ""
                except Exception as exc:
                    self.last_error = f"{exc.__class__.__name__}: {exc}"
                    log.warning("mail poll failed: %s", exc)
                self.last_poll = time.strftime("%Y-%m-%d %H:%M:%S")
            await asyncio.sleep(max(15, int(cfg.get("poll_seconds") or 60)))

    async def poll_once(self, cfg: dict | None = None) -> int:
        cfg = cfg or self.config()
        if cfg.get("backend") == "imap_smtp":
            messages = await asyncio.to_thread(self._imap_fetch, cfg)
        else:
            messages = await self._graph_fetch(cfg)
        return sum(1 for m in messages if self.ingest(m, cfg))

    def ingest(self, message: dict, cfg: dict) -> bool:
        """Turn one inbound mail into a task. Returns True if a task was created."""
        mailbox = str(cfg.get("mailbox") or "").lower()
        if message["auto"] or message["from_addr"] == mailbox:
            return False
        if not sender_allowed(message["from_addr"], str(cfg.get("allowed_senders") or "")):
            self.ignored += 1
            log.info("ignored mail from %s (not on the allow-list)", message["from_addr"])
            return False
        subject = clean_subject(message["subject"])
        body = strip_quoted(message["body"])[:60000]
        agent_id, subject_rest = pick_agent(self.rt, subject, str(cfg.get("default_agent") or ""))
        title = subject_rest or (body.splitlines()[0][:120] if body else "(no subject)")
        self.rt.orchestrator.create_task(
            title=title, description=body or title, agent_id=agent_id, source="email",
            source_ref={"backend": cfg.get("backend"), "message_id": message["id"], "from": message["from_addr"],
                        "subject": message["subject"], "rfc_message_id": message.get("message_id", ""),
                        "references": message.get("references", [])},
            requester=f"{message['from_name']} <{message['from_addr']}>", thread_key=f"email:{message['thread']}",
        )
        return True

    # ------------------------------------------------------------------
    # IMAP / SMTP
    # ------------------------------------------------------------------
    def _imap_fetch(self, cfg: dict) -> list[dict]:
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg.get("imap_port") or 993))
        try:
            conn.login(cfg["username"], cfg.get("password", ""))
            conn.select("INBOX")
            _, data = conn.search(None, "UNSEEN")
            out = []
            for num in (data[0] or b"").split()[:25]:
                _, parts = conn.fetch(num, "(RFC822)")  # fetching marks the message as read
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    out.append(parse_rfc822(raw))
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _smtp_send(self, cfg: dict, to: list[str], subject: str, body: str, in_reply_to: str, references: list[str]) -> None:
        msg = EmailMessage()
        msg["From"] = cfg["mailbox"]
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Auto-Submitted"] = "auto-generated"  # stops other auto-responders from answering us
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = " ".join([*references, in_reply_to])
        msg.set_content(body)
        msg.add_alternative(
            "<html><body><pre style=\"font-family:Consolas,Menlo,monospace;font-size:13px;white-space:pre-wrap\">"
            + html.escape(body) + "</pre></body></html>", subtype="html",
        )
        host, port = cfg["smtp_host"], int(cfg.get("smtp_port") or 587)
        security = cfg.get("smtp_security") or "starttls"
        server = smtplib.SMTP_SSL(host, port, timeout=30) if security == "ssl" else smtplib.SMTP(host, port, timeout=30)
        try:
            if security == "starttls":
                server.starttls()
            if cfg.get("username"):
                server.login(cfg["username"], cfg.get("password", ""))
            server.send_message(msg)
        finally:
            server.quit()

    # ------------------------------------------------------------------
    # Microsoft Graph
    # ------------------------------------------------------------------
    async def _graph_headers(self, cfg: dict) -> dict:
        if not (self._graph_token and self._graph_token[0] > time.time() + 60):
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://login.microsoftonline.com/{cfg['tenant_id']}/oauth2/v2.0/token",
                    data={"grant_type": "client_credentials", "client_id": cfg["client_id"],
                          "client_secret": cfg.get("client_secret", ""), "scope": "https://graph.microsoft.com/.default"},
                )
            if resp.status_code != 200:
                raise RuntimeError(f"Graph token request failed: HTTP {resp.status_code} {resp.text[:200]}")
            data = resp.json()
            self._graph_token = (time.time() + int(data.get("expires_in", 3600)), data["access_token"])
        return {"Authorization": f"Bearer {self._graph_token[1]}"}

    async def _graph_fetch(self, cfg: dict) -> list[dict]:
        headers = {**await self._graph_headers(cfg), "Prefer": 'outlook.body-content-type="text"'}
        base = f"{GRAPH}/users/{cfg['mailbox']}"
        out = []
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/mailFolders/inbox/messages",
                params={"$filter": "isRead eq false", "$top": "25",
                        "$select": "id,subject,from,body,conversationId,internetMessageId,internetMessageHeaders"},
                headers=headers,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Graph mail read failed: HTTP {resp.status_code} {resp.text[:200]}")
            for item in resp.json().get("value", []):
                sender = (item.get("from") or {}).get("emailAddress") or {}
                hdrs = {h["name"].lower(): h["value"] for h in item.get("internetMessageHeaders") or []}
                auto = hdrs.get("auto-submitted", "no").lower() != "no" or \
                    hdrs.get("precedence", "").lower() in {"bulk", "junk", "auto_reply", "list"} or \
                    "x-auto-response-suppress" in hdrs
                out.append({
                    "id": item["id"], "from_addr": (sender.get("address") or "").lower(),
                    "from_name": sender.get("name") or sender.get("address") or "", "subject": item.get("subject") or "",
                    "body": (item.get("body") or {}).get("content") or "", "message_id": item.get("internetMessageId", ""),
                    "references": [], "auto": auto, "thread": item.get("conversationId") or item["id"],
                })
                await client.patch(f"{base}/messages/{item['id']}", json={"isRead": True}, headers=headers)
        return out

    async def _graph_send(self, cfg: dict, to: list[str], subject: str, body: str, reply_to_id: str = "") -> None:
        headers = await self._graph_headers(cfg)
        base = f"{GRAPH}/users/{cfg['mailbox']}"
        async with httpx.AsyncClient(timeout=30) as client:
            if reply_to_id:
                resp = await client.post(f"{base}/messages/{reply_to_id}/reply", json={"comment": body}, headers=headers)
            else:
                resp = await client.post(f"{base}/sendMail", headers=headers, json={
                    "message": {"subject": subject, "body": {"contentType": "Text", "content": body},
                                "toRecipients": [{"emailAddress": {"address": a}} for a in to]},
                    "saveToSentItems": True,
                })
        if resp.status_code >= 300:
            raise RuntimeError(f"Graph send failed: HTTP {resp.status_code} {resp.text[:200]}")

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------
    async def send(self, to: list[str], subject: str, body: str, in_reply_to: str = "",
                   references: list[str] | None = None, graph_reply_id: str = "") -> None:
        cfg = self.config()
        if cfg.get("backend") == "imap_smtp":
            await asyncio.to_thread(self._smtp_send, cfg, to, subject, body, in_reply_to, references or [])
        else:
            await self._graph_send(cfg, to, subject, body, graph_reply_id)

    async def deliver_result(self, task: dict) -> bool:
        ref = task["source_ref"] or {}
        if not ref.get("from") or not self.config().get("enabled"):
            return False
        try:
            await self.send(
                [ref["from"]], "Re: " + clean_subject(ref.get("subject") or task["title"]),
                format_result(self.rt, task, limit=100000), in_reply_to=ref.get("rfc_message_id", ""),
                references=ref.get("references") or [],
                graph_reply_id=ref.get("message_id", "") if ref.get("backend") == "graph" else "",
            )
            self.last_error = ""
            return True
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("mail reply failed: %s", exc)
            return False

    async def test(self) -> tuple[bool, str]:
        cfg = self.config()
        if not cfg.get("mailbox"):
            return False, "set the agent mailbox address"
        try:
            if cfg.get("backend") == "imap_smtp":
                await asyncio.to_thread(self._imap_check, cfg)
                return True, "IMAP login succeeded"
            headers = await self._graph_headers(cfg)
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"{GRAPH}/users/{cfg['mailbox']}/mailFolders/inbox", headers=headers)
            if resp.status_code != 200:
                return False, f"Graph could not open the mailbox: HTTP {resp.status_code} {resp.text[:160]}"
            return True, "Graph can read the mailbox"
        except Exception as exc:
            return False, f"{exc.__class__.__name__}: {exc}"

    def _imap_check(self, cfg: dict) -> None:
        conn = imaplib.IMAP4_SSL(cfg["imap_host"], int(cfg.get("imap_port") or 993))
        try:
            conn.login(cfg["username"], cfg.get("password", ""))
        finally:
            try:
                conn.logout()
            except Exception:
                pass
