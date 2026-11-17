"""Microsoft Teams, email and notification channels (no network access: external calls are stubbed)."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from netagent.api import create_app
from netagent.channels import adaptive_card
from netagent.channels.common import handle_command, pick_agent
from netagent.channels.email import clean_subject, html_to_text, parse_rfc822, sender_allowed, strip_quoted
from netagent.channels.teams import clean_text, verify_outgoing_hmac

ADMIN = {"Authorization": "Bearer test-admin-token"}
SECRET = base64.b64encode(b"teams-shared-secret-0123456789ab").decode()


def sign(body: bytes) -> str:
    return "HMAC " + base64.b64encode(hmac.new(base64.b64decode(SECRET), body, hashlib.sha256).digest()).decode()


@pytest.fixture
def client(rt):
    with TestClient(create_app(rt)) as c:
        yield c


# ------------------------------------------------------------------ teams
def test_outgoing_webhook_hmac():
    body = b'{"text": "hello"}'
    assert verify_outgoing_hmac(SECRET, body, sign(body))
    assert not verify_outgoing_hmac(SECRET, body + b" ", sign(body))
    assert not verify_outgoing_hmac(SECRET, body, "HMAC AAAA")
    assert not verify_outgoing_hmac("", body, sign(body))
    assert not verify_outgoing_hmac(SECRET, body, "Bearer x")


def test_teams_text_cleanup():
    activity = {"text": "<at>NetAgent</at> Cisco Expert: check <b>sw-core-01</b>&nbsp;please",
                "entities": [{"type": "mention", "text": "<at>NetAgent</at>"}]}
    assert clean_text(activity) == "Cisco Expert: check sw-core-01 please"


def test_teams_outgoing_webhook_creates_task_and_answers_inline(client, rt):
    body = json.dumps({"type": "message", "text": "<at>NetAgent</at> What does ARP do?", "from": {"name": "Dana"},
                       "conversation": {"id": "19:abc"}}).encode()
    # disabled by default
    assert client.post("/hooks/teams/outgoing", content=body, headers={"Authorization": sign(body)}).status_code == 404
    client.put("/api/v1/channels/teams", json={"enabled": True, "mode": "outgoing_webhook", "outgoing_secret": SECRET}, headers=ADMIN)

    bad = client.post("/hooks/teams/outgoing", content=body, headers={"Authorization": "HMAC forged"})
    assert bad.status_code == 401 and rt.db.scalar("SELECT COUNT(*) FROM tasks") == 0

    resp = client.post("/hooks/teams/outgoing", content=body, headers={"Authorization": sign(body)})
    assert resp.status_code == 200
    reply = resp.json()
    assert reply["type"] == "message" and "Demo answer" in reply["text"]  # finished within the inline window
    task = rt.db.one("SELECT * FROM tasks")
    assert task["source"] == "teams" and task["requester"] == "Dana" and task["thread_key"] == "teams:19:abc"

    helper = json.dumps({"type": "message", "text": "<at>NetAgent</at> agents"}).encode()
    listing = client.post("/hooks/teams/outgoing", content=helper, headers={"Authorization": sign(helper)}).json()["text"]
    assert "Fortinet Expert" in listing and rt.db.scalar("SELECT COUNT(*) FROM tasks") == 1  # commands create no task


def test_teams_bot_rejects_unauthenticated_requests(client, rt):
    activity = {"type": "message", "text": "do something", "serviceUrl": "https://evil.example/"}
    assert client.post("/hooks/teams/messages", json=activity).status_code == 404  # not enabled
    client.put("/api/v1/channels/teams", json={"enabled": True, "mode": "bot", "app_id": "app-123", "app_password": "pw"}, headers=ADMIN)
    assert client.post("/hooks/teams/messages", json=activity).status_code == 401
    assert client.post("/hooks/teams/messages", json=activity, headers={"Authorization": "Bearer not.a.jwt"}).status_code == 401
    assert rt.db.scalar("SELECT COUNT(*) FROM tasks") == 0


def test_teams_bot_flow_with_valid_token(client, rt, monkeypatch):
    """Token validation and the Bot Connector are stubbed; everything in between is real."""
    teams = rt.channels.teams
    sent = []
    monkeypatch.setattr(teams, "_validate_jwt", lambda token, cfg: {"serviceurl": "https://smba.example/amer/"})

    async def fake_send(ref, text):
        sent.append((ref, text))

    monkeypatch.setattr(teams, "send", fake_send)
    client.put("/api/v1/channels/teams", json={"enabled": True, "mode": "bot", "app_id": "app-123", "app_password": "pw",
                                               "allowed_tenants": "tenant-good"}, headers=ADMIN)
    activity = {"type": "message", "id": "act-1", "text": "<at>NetAgent</at> Arista Expert: What does ARP do?",
                "serviceUrl": "https://smba.example/amer/", "from": {"id": "u1", "name": "Sam"}, "recipient": {"id": "bot"},
                "conversation": {"id": "conv-9"}, "channelData": {"tenant": {"id": "tenant-good"}},
                "entities": [{"type": "mention", "text": "<at>NetAgent</at>"}]}
    auth = {"Authorization": "Bearer stubbed"}

    wrong_tenant = {**activity, "channelData": {"tenant": {"id": "tenant-other"}}}
    assert client.post("/hooks/teams/messages", json=wrong_tenant, headers=auth).status_code == 403
    spoofed = {**activity, "serviceUrl": "https://attacker.example/"}
    assert client.post("/hooks/teams/messages", json=spoofed, headers=auth).status_code == 401
    assert rt.db.scalar("SELECT COUNT(*) FROM tasks") == 0

    assert client.post("/hooks/teams/messages", json=activity, headers=auth).status_code == 200
    task = rt.db.one("SELECT * FROM tasks")
    assert task["description"] == "What does ARP do?" and task["requester"] == "Sam"
    assert rt.db.get("agents", task["agent_id"])["name"] == "Arista Expert"
    deadline = time.time() + 10
    while time.time() < deadline and len(sent) < 2:
        time.sleep(0.05)
    assert "is with **Arista Expert**" in sent[0][1]  # acknowledgement
    ref, result = sent[1]  # completion posted back to the same conversation
    assert ref["conversation_id"] == "conv-9" and ref["activity_id"] == "act-1" and "Demo answer" in result
    events = [e["summary"] for e in rt.db.query("SELECT summary FROM task_events WHERE task_id = ?", [task["id"]])]
    assert any("delivered to the requester via teams" in e for e in events)


# ------------------------------------------------------------------ email
def test_sender_allow_list():
    allowed = "noc@example.com, @corp.example"
    assert sender_allowed("noc@example.com", allowed)
    assert sender_allowed("Anyone@Corp.Example", allowed)
    assert not sender_allowed("noc@example.com.evil.net", allowed)
    assert not sender_allowed("someone@example.com", allowed)
    assert not sender_allowed("x@corp.example.evil.org", allowed)
    assert not sender_allowed("noc@example.com", "")  # empty list accepts nobody
    assert not sender_allowed("", allowed)


def test_mail_text_helpers():
    assert clean_subject("RE: Fwd: re: BGP down") == "BGP down"
    assert html_to_text("<p>Line one</p><div>Line&nbsp;two &amp; three</div><script>x()</script>").split() == [
        "Line", "one", "Line", "two", "&", "three"]
    body = "New question here.\n\n> old quoted line\nOn Mon, 3 Nov 2026 at 10:00, NetAgent <a@b.c> wrote:\n> earlier answer"
    assert strip_quoted(body) == "New question here."
    outlook = "Thanks, one more thing.\n\nFrom: NetAgent\nSent: Monday\nTo: me\nSubject: x\n\nold"
    assert strip_quoted(outlook) == "Thanks, one more thing."
    assert "From: 10.1.1.1 to 10.2.2.2" in strip_quoted("Trace:\nFrom: 10.1.1.1 to 10.2.2.2\nhop 1")


RAW_MAIL = b"""From: Pat Engineer <pat@corp.example>
To: netagent@corp.example
Subject: [Fortinet Expert] VPN-DR tunnel down
Message-ID: <m1@corp.example>
Content-Type: text/plain; charset=utf-8

The VPN-DR tunnel on fw-edge-01 is down since 09:00.
"""


def test_email_ingest(rt):
    email = rt.channels.email
    cfg = {"mailbox": "netagent@corp.example", "allowed_senders": "@corp.example", "backend": "imap_smtp"}
    message = parse_rfc822(RAW_MAIL)
    assert message["from_addr"] == "pat@corp.example" and not message["auto"]
    assert email.ingest(message, cfg)
    task = rt.db.one("SELECT * FROM tasks")
    assert task["source"] == "email" and task["title"] == "VPN-DR tunnel down"
    assert "down since 09:00" in task["description"] and task["requester"] == "Pat Engineer <pat@corp.example>"
    assert rt.db.get("agents", task["agent_id"])["name"] == "Fortinet Expert"
    assert task["source_ref"]["from"] == "pat@corp.example" and task["thread_key"] == "email:<m1@corp.example>"

    outsider = parse_rfc822(RAW_MAIL.replace(b"pat@corp.example", b"mallory@evil.example"))
    auto = parse_rfc822(RAW_MAIL.replace(b"Subject:", b"Auto-Submitted: auto-replied\nSubject:"))
    own = parse_rfc822(RAW_MAIL.replace(b"Pat Engineer <pat@corp.example>", b"netagent@corp.example"))
    assert not email.ingest(outsider, cfg) and not email.ingest(auto, cfg) and not email.ingest(own, cfg)
    assert rt.db.scalar("SELECT COUNT(*) FROM tasks") == 1 and email.ignored == 1


async def test_email_reply_on_completion(rt, monkeypatch):
    sent = []

    async def fake_send(to, subject, body, in_reply_to="", references=None, graph_reply_id=""):
        sent.append({"to": to, "subject": subject, "body": body, "in_reply_to": in_reply_to})

    monkeypatch.setattr(rt.channels.email, "send", fake_send)
    rt.channels.set_config("email", {"enabled": True, "backend": "imap_smtp", "mailbox": "netagent@corp.example",
                                     "allowed_senders": "@corp.example"})
    await rt.orchestrator.start()
    try:
        rt.channels.email.ingest(parse_rfc822(RAW_MAIL), rt.channels.get_config("email"))
        task = rt.db.one("SELECT * FROM tasks")
        await rt.orchestrator.wait_for(task["id"], 10)
        for _ in range(100):
            if sent:
                break
            import asyncio
            await asyncio.sleep(0.05)
    finally:
        await rt.orchestrator.stop()
    assert sent[0]["to"] == ["pat@corp.example"] and sent[0]["subject"] == "Re: [Fortinet Expert] VPN-DR tunnel down"
    assert sent[0]["in_reply_to"] == "<m1@corp.example>" and f"Task #{task['id']}" in sent[0]["body"]


# --------------------------------------------------- notifications/config
def test_channel_secrets_are_encrypted_and_masked(client, rt):
    client.put("/api/v1/channels/teams", json={"enabled": True, "app_password": "super-secret-pw", "app_id": "abc"}, headers=ADMIN)
    listing = client.get("/api/v1/channels", headers=ADMIN)
    assert "super-secret-pw" not in listing.text
    teams = next(c for c in listing.json() if c["id"] == "teams")
    assert teams["values"]["app_password"] == "********" and teams["values"]["app_id"] == "abc"
    assert "super-secret-pw" not in json.dumps(rt.db.get_setting("channel.teams"))
    assert rt.channels.get_config("teams")["app_password"] == "super-secret-pw"
    # saving the form back with the mask keeps the stored secret
    client.put("/api/v1/channels/teams", json={"app_password": "********", "app_id": "xyz"}, headers=ADMIN)
    cfg = rt.channels.get_config("teams")
    assert cfg["app_password"] == "super-secret-pw" and cfg["app_id"] == "xyz"


async def test_completion_notification_and_callback(rt, monkeypatch):
    posts = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            posts.append((url, json))
            return FakeResponse()

    import netagent.channels as channels
    monkeypatch.setattr(channels.httpx, "AsyncClient", FakeClient)
    rt.channels.set_config("notifications", {"teams_webhook_url": "https://hooks.example/teams", "on_completed": True})
    await rt.orchestrator.start()
    try:
        task = rt.orchestrator.create_task("What does ARP do?", "", source="api", source_ref={"callback_url": "https://itsm.example/cb"})
        await rt.orchestrator.wait_for(task["id"], 10)
        import asyncio
        for _ in range(100):
            if len(posts) >= 2:
                break
            await asyncio.sleep(0.05)
        # agent sub-tasks do not notify people
        child = rt.orchestrator.create_task("sub", "", source="agent", parent_id=task["id"])
        await rt.orchestrator.wait_for(child["id"], 10)
        await asyncio.sleep(0.2)
    finally:
        await rt.orchestrator.stop()
    by_url = dict(posts)
    assert len(posts) == 2
    assert by_url["https://itsm.example/cb"]["status"] == "completed" and by_url["https://itsm.example/cb"]["task_id"] == task["id"]
    card = by_url["https://hooks.example/teams"]["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert "Completed: #" in card["content"]["body"][0]["text"]
    assert card["content"]["actions"][0]["url"].endswith(f"/#/tasks/{task['id']}")


def test_adaptive_card_shape():
    card = adaptive_card("Title", [("Agent", "Cisco Expert"), ("Empty", "")], "Body", "https://x/#/tasks/1", "good")
    content = card["attachments"][0]["content"]
    assert card["type"] == "message" and content["type"] == "AdaptiveCard"
    assert content["body"][1]["facts"] == [{"title": "Agent", "value": "Cisco Expert"}]


def test_chat_commands_and_agent_addressing(rt):
    assert "NetAgent" in handle_command(rt, "help")
    assert "Cisco Expert" in handle_command(rt, "agents")
    assert handle_command(rt, "tasks") == "No open tasks."
    assert "no task #77" in handle_command(rt, "status 77")
    assert handle_command(rt, "why is bgp down") is None
    cisco = rt.db.one("SELECT id FROM agents WHERE name = 'Cisco Expert'")["id"]
    assert pick_agent(rt, "cisco expert: check vlan 10") == (cisco, "check vlan 10")
    assert pick_agent(rt, "@Cisco Expert check vlan 10") == (cisco, "check vlan 10")
    assert pick_agent(rt, "[Cisco Expert] check vlan 10") == (cisco, "check vlan 10")
    assert pick_agent(rt, "check vlan 10") == (None, "check vlan 10")
    assert pick_agent(rt, "check vlan 10", default_agent="Cisco Expert") == (cisco, "check vlan 10")
