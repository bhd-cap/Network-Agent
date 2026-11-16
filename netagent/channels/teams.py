"""Microsoft Teams.

Two ways in, one way out:

* **Bot** (recommended, two-way): an Azure Bot registration whose messaging
  endpoint is ``<public url>/hooks/teams/messages``. Requests are authenticated
  by validating the Bot Framework JWT; results are posted back into the same
  conversation when the task finishes.
* **Outgoing webhook** (no Azure setup, @mention only): Teams calls
  ``<public url>/hooks/teams/outgoing`` and authenticates with an HMAC of the
  body. The reply must be immediate, so fast answers are returned inline and
  slower ones are delivered through the notifications webhook.
* **Notifications**: an incoming-webhook / Workflows URL that receives a card
  when tasks finish or a change needs approval (configured under Notifications).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re
import time
from typing import TYPE_CHECKING

import httpx

from .common import format_result, handle_command, pick_agent

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

log = logging.getLogger("netagent.teams")

BOT_OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_ISSUER = "https://api.botframework.com"
BOT_SCOPE = "https://api.botframework.com/.default"
_AT = re.compile(r"<at>.*?</at>", re.S | re.I)

SCHEMA = [
    {"key": "enabled", "label": "Enable Teams", "type": "bool"},
    {"key": "mode", "label": "Connection type", "type": "select", "options": ["bot", "outgoing_webhook"],
     "help": "bot = Azure Bot registration (two-way). outgoing_webhook = Teams outgoing webhook (@mention, no Azure)."},
    {"key": "app_id", "label": "Bot: Microsoft App ID", "type": "text"},
    {"key": "app_password", "label": "Bot: client secret", "type": "secret"},
    {"key": "tenant_id", "label": "Bot: Directory (tenant) ID", "type": "text",
     "help": "Required for single-tenant bots. Leave empty for a multi-tenant bot."},
    {"key": "allowed_tenants", "label": "Accept messages only from these tenant IDs", "type": "text",
     "help": "Comma separated. Strongly recommended for bots."},
    {"key": "outgoing_secret", "label": "Outgoing webhook: security token", "type": "secret",
     "help": "Shown once by Teams when the outgoing webhook is created."},
    {"key": "default_agent", "label": "Default agent", "type": "text",
     "help": "Agent name for messages that do not address an agent. Empty = automatic routing."},
]
DEFAULTS = {"enabled": False, "mode": "bot"}


def verify_outgoing_hmac(secret_b64: str, body: bytes, auth_header: str) -> bool:
    """Teams signs outgoing-webhook requests: Authorization: HMAC base64(hmac_sha256(key, body))."""
    if not secret_b64 or not auth_header.startswith("HMAC "):
        return False
    try:
        key = base64.b64decode(secret_b64)
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(expected, auth_header[5:].strip())


def clean_text(activity: dict) -> str:
    text = activity.get("text") or ""
    for entity in activity.get("entities") or []:
        if entity.get("type") == "mention" and entity.get("text"):
            text = text.replace(entity["text"], " ")
    text = _AT.sub(" ", text)
    text = re.sub(r"<[^>]+>", " ", text)  # outgoing webhooks send HTML
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"[ \t]+", " ", text).strip()


class TeamsChannel:
    def __init__(self, rt: "Runtime"):
        self.rt = rt
        self._token: tuple[float, str] | None = None
        self._jwks_client = None
        self.last_error = ""

    def config(self) -> dict:
        return self.rt.channels.get_config("teams")

    # ------------------------------------------------------------------
    # inbound: bot
    # ------------------------------------------------------------------
    def _validate_jwt(self, token: str, cfg: dict) -> dict:
        import jwt

        if self._jwks_client is None:
            meta = httpx.get(BOT_OPENID_URL, timeout=10).json()
            self._jwks_client = jwt.PyJWKClient(meta["jwks_uri"], cache_keys=True, lifespan=24 * 3600)
        key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=cfg["app_id"], issuer=BOT_ISSUER, leeway=300)

    async def handle_bot(self, auth_header: str, activity: dict) -> tuple[int, dict]:
        cfg = self.config()
        if not cfg.get("enabled") or cfg.get("mode") != "bot" or not cfg.get("app_id"):
            return 404, {"error": "Teams bot is not enabled"}
        if not auth_header.lower().startswith("bearer "):
            return 401, {"error": "missing bearer token"}
        try:
            claims = await asyncio.to_thread(self._validate_jwt, auth_header[7:].strip(), cfg)
        except Exception as exc:
            self.last_error = f"rejected bot request: {exc.__class__.__name__}"
            log.warning("Teams JWT validation failed: %s", exc)
            return 401, {"error": "invalid token"}
        service_url = activity.get("serviceUrl") or ""
        if claims.get("serviceurl") and claims["serviceurl"].rstrip("/") != service_url.rstrip("/"):
            return 401, {"error": "serviceUrl mismatch"}
        tenant = ((activity.get("channelData") or {}).get("tenant") or {}).get("id") or \
            (activity.get("conversation") or {}).get("tenantId") or ""
        allowed = [t.strip().lower() for t in str(cfg.get("allowed_tenants") or "").split(",") if t.strip()]
        if allowed and tenant.lower() not in allowed:
            log.warning("Teams message from tenant %s refused", tenant)
            return 403, {"error": "tenant not allowed"}
        if activity.get("type") != "message":
            return 200, {}
        text = clean_text(activity)
        if not text:
            return 200, {}
        ref = {
            "mode": "bot", "service_url": service_url,
            "conversation_id": (activity.get("conversation") or {}).get("id", ""),
            "activity_id": activity.get("id", ""),
            "bot": activity.get("recipient") or {}, "user": activity.get("from") or {},
        }
        reply = handle_command(self.rt, text)
        if reply is None:
            task = self._create_task(text, cfg, ref, (activity.get("from") or {}).get("name", "Teams user"))
            agent = self.rt.db.get("agents", task["agent_id"]) if task["agent_id"] else None
            reply = (f"Task **#{task['id']}** is with **{agent['name']}**." if agent
                     else f"Task **#{task['id']}** is queued - no agent is available yet.") + " I'll post the result here."
        asyncio.create_task(self._send_safe(ref, reply))
        return 200, {}

    def _create_task(self, text: str, cfg: dict, ref: dict, requester: str) -> dict:
        agent_id, body = pick_agent(self.rt, text, str(cfg.get("default_agent") or ""))
        return self.rt.orchestrator.create_task(
            title=body.splitlines()[0][:120], description=body, agent_id=agent_id, source="teams", source_ref=ref,
            requester=requester, thread_key=f"teams:{ref.get('conversation_id') or requester}",
        )

    # ------------------------------------------------------------------
    # inbound: outgoing webhook
    # ------------------------------------------------------------------
    async def handle_outgoing(self, auth_header: str, raw_body: bytes, activity: dict) -> tuple[int, dict]:
        cfg = self.config()
        if not cfg.get("enabled") or cfg.get("mode") != "outgoing_webhook":
            return 404, {"error": "Teams outgoing webhook is not enabled"}
        if not verify_outgoing_hmac(str(cfg.get("outgoing_secret") or ""), raw_body, auth_header):
            self.last_error = "rejected outgoing-webhook request: bad HMAC"
            return 401, {"type": "message", "text": "Signature check failed."}
        text = clean_text(activity)
        if not text:
            return 200, {"type": "message", "text": "Send me a question or a task. Say `help` for commands."}
        reply = handle_command(self.rt, text)
        if reply is not None:
            return 200, {"type": "message", "text": reply}
        requester = (activity.get("from") or {}).get("name", "Teams user")
        ref = {"mode": "outgoing", "conversation_id": (activity.get("conversation") or {}).get("id", "")}
        task = self._create_task(text, cfg, ref, requester)
        # Teams needs the HTTP response within seconds: return quick answers inline.
        done = await self.rt.orchestrator.wait_for(task["id"], 3.5)
        if done and done["status"] in {"completed", "failed"}:
            self.rt.db.update("tasks", task["id"], {"source_ref": {**ref, "answered_inline": True}})
            return 200, {"type": "message", "text": format_result(self.rt, done)}
        return 200, {"type": "message", "text": (
            f"Task **#{task['id']}** is in progress. The result will be posted by the notifications webhook, "
            f"or ask me `status {task['id']}`."
        )}

    # ------------------------------------------------------------------
    # outbound
    # ------------------------------------------------------------------
    async def _bot_token(self, cfg: dict) -> str:
        if self._token and self._token[0] > time.time() + 60:
            return self._token[1]
        tenant = str(cfg.get("tenant_id") or "").strip() or "botframework.com"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={"grant_type": "client_credentials", "client_id": cfg["app_id"],
                      "client_secret": cfg.get("app_password", ""), "scope": BOT_SCOPE},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"bot token request failed: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        self._token = (time.time() + int(data.get("expires_in", 3600)), data["access_token"])
        return self._token[1]

    async def send(self, ref: dict, text: str) -> None:
        cfg = self.config()
        token = await self._bot_token(cfg)
        base = ref["service_url"].rstrip("/")
        url = f"{base}/v3/conversations/{ref['conversation_id']}/activities"
        payload = {
            "type": "message", "text": text, "textFormat": "markdown", "from": ref.get("bot") or {},
            "recipient": ref.get("user") or {}, "conversation": {"id": ref["conversation_id"]},
        }
        if ref.get("activity_id"):
            url += f"/{ref['activity_id']}"
            payload["replyToId"] = ref["activity_id"]
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 300:
            raise RuntimeError(f"Teams reply failed: HTTP {resp.status_code} {resp.text[:200]}")

    async def _send_safe(self, ref: dict, text: str) -> None:
        try:
            await self.send(ref, text)
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("Teams send failed: %s", exc)

    async def deliver_result(self, task: dict) -> bool:
        """Post a finished task back to where it came from. Returns True when delivered."""
        ref = task["source_ref"] or {}
        if ref.get("mode") != "bot" or not ref.get("service_url"):
            return False
        await self._send_safe(ref, format_result(self.rt, task))
        return not self.last_error

    async def test(self) -> tuple[bool, str]:
        cfg = self.config()
        if cfg.get("mode") == "outgoing_webhook":
            ok = bool(cfg.get("outgoing_secret"))
            return ok, "security token is set; create the webhook in Teams pointing at /hooks/teams/outgoing" if ok \
                else "set the security token Teams showed when the outgoing webhook was created"
        if not cfg.get("app_id") or not cfg.get("app_password"):
            return False, "App ID and client secret are required"
        try:
            self._token = None
            await self._bot_token(cfg)
            return True, "bot credentials accepted by Microsoft identity platform"
        except Exception as exc:
            return False, str(exc)
