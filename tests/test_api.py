"""REST API: authentication, task lifecycle, management endpoints and the approval flow."""
import time

import pytest
from fastapi.testclient import TestClient

from netagent.api import create_app

ADMIN = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
def client(rt):
    with TestClient(create_app(rt)) as c:
        yield c


def wait_task(client, task_id, headers=ADMIN, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()
        if task["status"] in {"completed", "failed", "cancelled"}:
            return task
        time.sleep(0.05)
    raise AssertionError("task did not finish")


# ------------------------------------------------------------------ auth
def test_health_is_public_everything_else_needs_auth(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    for path in ("/api/v1/tasks", "/api/v1/agents", "/api/v1/models", "/api/v1/overview", "/api/v1/events",
                 "/api/v1/channels", "/api/v1/keys", "/api/v1/devices", "/api/v1/changes", "/api/v1/settings",
                 "/api/v1/reports/summary", "/api/v1/reports/tasks.csv"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/tasks", headers=ADMIN).status_code == 200


def test_cookie_session_and_csrf_header(client):
    assert client.post("/api/v1/auth/login", json={"token": "nope"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"token": "test-admin-token"}).status_code == 200
    assert client.get("/api/v1/auth/me").json()["scope"] == "admin"
    # A cross-site form post cannot add the custom header, so cookie-authenticated writes without it are refused.
    assert client.post("/api/v1/tasks", json={"title": "x"}).status_code == 401
    assert client.post("/api/v1/tasks", json={"title": "x"}, headers={"X-Requested-With": "netagent"}).status_code == 201
    client.post("/api/v1/auth/logout", headers={"X-Requested-With": "netagent"})
    assert client.get("/api/v1/auth/me").status_code == 401


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/v1/auth/login", json={"token": "bad"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"token": "test-admin-token"}).status_code == 429


def test_api_key_scopes(client):
    created = client.post("/api/v1/keys", json={"name": "itsm", "scope": "tasks"}, headers=ADMIN).json()
    key = {"Authorization": f"Bearer {created['key']}"}
    assert created["key"].startswith("na_")
    assert client.post("/api/v1/tasks", json={"title": "What does ARP do?"}, headers=key).status_code == 201
    assert client.get("/api/v1/tasks", headers={"X-API-Key": created["key"]}).status_code == 200
    for path in ("/api/v1/models", "/api/v1/devices", "/api/v1/channels", "/api/v1/keys", "/api/v1/changes"):
        assert client.get(path, headers=key).status_code == 403, path
    assert client.post("/api/v1/agents", json={"name": "x", "profile_id": "cisco-expert"}, headers=key).status_code == 403
    listed = client.get("/api/v1/keys", headers=ADMIN).json()
    assert listed[0]["name"] == "itsm" and "key" not in listed[0] and "key_hash" not in listed[0]
    client.delete(f"/api/v1/keys/{created['id']}", headers=ADMIN)
    assert client.get("/api/v1/tasks", headers=key).status_code == 401


# ----------------------------------------------------------------- tasks
def test_task_lifecycle_and_report(client):
    resp = client.post("/api/v1/tasks", json={"title": "Usable hosts in 10.9.8.0/22", "description": "How many hosts in 10.9.8.0/22?",
                                              "agent": "Cisco Expert", "priority": 2}, headers=ADMIN)
    assert resp.status_code == 201
    task = wait_task(client, resp.json()["id"])
    assert task["status"] == "completed" and task["agent_name"] == "Cisco Expert" and task["source"] == "api"
    assert "1022" in task["result"]
    assert any(e["kind"] == "tool" and "subnet_calc" in e["summary"] for e in task["events"])
    report = client.get(f"/api/v1/tasks/{task['id']}/report", headers=ADMIN).text
    assert "# Task #" in report and "## Timeline" in report and "subnet_calc" in report
    assert client.post(f"/api/v1/tasks/{task['id']}/cancel", headers=ADMIN).status_code == 409
    assert client.post(f"/api/v1/tasks/{task['id']}/retry", headers=ADMIN).status_code == 200
    assert wait_task(client, task["id"])["status"] == "completed"
    listed = client.get("/api/v1/tasks?status=completed&q=10.9.8", headers=ADMIN).json()
    assert [t["id"] for t in listed] == [task["id"]]


def test_task_validation(client):
    assert client.post("/api/v1/tasks", json={}, headers=ADMIN).status_code == 422
    assert client.post("/api/v1/tasks", json={"title": "x", "agent": "Nobody"}, headers=ADMIN).status_code == 404
    assert client.post("/api/v1/tasks", json={"title": "x", "callback_url": "file:///etc/passwd"}, headers=ADMIN).status_code == 422
    assert client.get("/api/v1/tasks/99999", headers=ADMIN).status_code == 404


def test_ask_waits_for_the_answer(client):
    resp = client.post("/api/v1/ask", json={"question": "What does ARP do?", "timeout": 10}, headers=ADMIN)
    assert resp.status_code == 200 and resp.json()["status"] == "completed" and resp.json()["result"]


def test_escalated_task_is_visible_in_reports(client):
    resp = client.post("/api/v1/tasks", json={"title": "Design a multi-vendor migration plan"}, headers=ADMIN)
    task = wait_task(client, resp.json()["id"])
    assert task["escalations"] == 1 and task["model_name"].startswith("Demo large")
    summary = client.get("/api/v1/reports/summary?days=1", headers=ADMIN).json()
    assert summary["totals"]["completed"] == 1 and summary["totals"]["escalated"] == 1
    assert summary["totals"]["escalation_rate"] == 100.0
    assert summary["by_model"][0]["name"].startswith("Demo large")
    csv_text = client.get("/api/v1/reports/tasks.csv", headers=ADMIN).text
    assert csv_text.splitlines()[0].startswith("id,parent_id,title") and "Design a multi-vendor migration plan" in csv_text
    assert "NetAgent report" in client.get("/api/v1/reports/digest", headers=ADMIN).text
    assert client.post("/api/v1/reports/send", headers=ADMIN).status_code == 409  # no targets configured


# ------------------------------------------------------------ management
def test_agent_and_profile_management(client):
    profile = client.post("/api/v1/profiles", json={
        "name": "Wireless Engineer", "description": "Wi-Fi", "skills": ["network-core"], "keywords": ["WiFi", " wlc "],
    }, headers=ADMIN)
    assert profile.status_code == 201 and profile.json()["id"] == "wireless-engineer"
    assert profile.json()["keywords"] == ["wifi", "wlc"] and profile.json()["builtin"] is False
    assert client.post("/api/v1/profiles", json={"name": "x", "skills": ["root-shell"]}, headers=ADMIN).status_code == 422
    assert client.put("/api/v1/profiles/cisco-expert", json={"name": "hacked"}, headers=ADMIN).status_code == 409

    agent = client.post("/api/v1/agents", json={"name": "Wifi Bot", "profile_id": "wireless-engineer"}, headers=ADMIN)
    assert agent.status_code == 201
    agent_id = agent.json()["id"]
    assert client.post("/api/v1/agents", json={"name": "wifi bot", "profile_id": "wireless-engineer"}, headers=ADMIN).status_code == 409
    assert client.delete("/api/v1/profiles/wireless-engineer", headers=ADMIN).status_code == 409  # in use
    routed = client.post("/api/v1/tasks", json={"title": "WLC upgrade plan"}, headers=ADMIN).json()
    assert routed["agent_name"] == "Wifi Bot"
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"status": "paused"}, headers=ADMIN).json()["status"] == "paused"
    assert client.patch(f"/api/v1/agents/{agent_id}", json={"status": "bogus"}, headers=ADMIN).status_code == 422
    assert client.delete(f"/api/v1/agents/{agent_id}", headers=ADMIN).status_code == 200
    assert client.delete("/api/v1/profiles/wireless-engineer", headers=ADMIN).status_code == 200
    names = [a["name"] for a in client.get("/api/v1/agents", headers=ADMIN).json()]
    assert names == ["Network Lead", "Cisco Expert", "Fortinet Expert", "Arista Expert"]


def test_model_and_ladder_management_hides_secrets(client, rt):
    resp = client.post("/api/v1/models", json={"name": "My Gateway", "provider": "openai_compatible", "model": "llama",
                                               "base_url": "http://10.0.0.5:8000/v1", "api_key": "sk-secret-value", "location": "local"},
                       headers=ADMIN)
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "my-gateway" and body["has_key"] is True
    assert "sk-secret-value" not in resp.text and "api_key_enc" not in body
    stored = rt.db.get("models", "my-gateway")["api_key_enc"]
    assert stored and "sk-secret-value" not in stored and rt.box.decrypt(stored) == "sk-secret-value"
    assert "sk-secret-value" not in client.get("/api/v1/models", headers=ADMIN).text

    ladder = client.post("/api/v1/ladders", json={"name": "Mine", "tiers": [{"model": "demo-small"}, {"model": "my-gateway"}],
                                                  "options": {"review": "always", "high_risk_min_tier": 9}}, headers=ADMIN)
    assert ladder.status_code == 201 and ladder.json()["options"]["high_risk_min_tier"] == 2
    assert client.post("/api/v1/ladders", json={"name": "Bad", "tiers": [{"model": "ghost"}]}, headers=ADMIN).status_code == 422
    assert client.delete("/api/v1/models/my-gateway", headers=ADMIN).status_code == 409  # used by a ladder
    assert client.put("/api/v1/settings", json={"default_ladder": "mine", "max_steps": 500}, headers=ADMIN).json() == {
        "default_ladder": "mine", "redact_secrets": True, "max_collab_depth": 3, "max_steps": 40}
    assert client.delete("/api/v1/ladders/mine", headers=ADMIN).status_code == 409  # default
    test = client.post("/api/v1/models/demo-small/test", headers=ADMIN).json()
    assert test["ok"] is True


def test_device_inventory_hides_passwords(client, rt):
    resp = client.post("/api/v1/devices", json={"name": "rtr-1", "host": "10.1.1.1", "platform": "cisco_xe",
                                                "username": "netops", "password": "Sup3rSecret!"}, headers=ADMIN)
    assert resp.status_code == 201 and resp.json()["has_password"] is True
    assert "Sup3rSecret" not in resp.text and "Sup3rSecret" not in client.get("/api/v1/devices", headers=ADMIN).text
    device_id = resp.json()["id"]
    # updating without a password keeps the stored one
    client.put(f"/api/v1/devices/{device_id}", json={"name": "rtr-1", "host": "10.1.1.2", "platform": "cisco_xe", "username": "netops"}, headers=ADMIN)
    assert rt.box.decrypt(rt.db.get("devices", device_id)["password_enc"]) == "Sup3rSecret!"
    assert client.post("/api/v1/devices", json={"name": "x", "platform": "juniper"}, headers=ADMIN).status_code == 422
    sim = next(d for d in client.get("/api/v1/devices", headers=ADMIN).json()["devices"] if d["name"] == "sw-core-01")
    assert client.post(f"/api/v1/devices/{sim['id']}/test", headers=ADMIN).json()["ok"] is True


# -------------------------------------------------------------- approvals
def test_config_change_needs_human_approval(client, rt):
    task = client.post("/api/v1/tasks", json={"title": "Add VLAN 120 named GUEST on sw-core-01", "agent": "Cisco Expert"}, headers=ADMIN).json()
    done = wait_task(client, task["id"])
    assert done["changes"][0]["status"] == "pending"
    change_id = done["changes"][0]["id"]
    pending = client.get("/api/v1/changes?status=pending", headers=ADMIN).json()
    assert pending[0]["commands"] == "vlan 120\n name GUEST" and pending[0]["rollback"] == "no vlan 120"

    assert client.post(f"/api/v1/changes/{change_id}/approve", json={}, headers=ADMIN).status_code == 200
    deadline = time.time() + 5
    while time.time() < deadline and rt.db.get("change_requests", change_id)["status"] == "approved":
        time.sleep(0.05)
    change = rt.db.get("change_requests", change_id)
    assert change["status"] == "applied" and change["decided_by"] == "admin"
    assert "vlan 120" in change["output"] and "show vlan id 120" in change["output"] and change["backup"]
    assert client.post(f"/api/v1/changes/{change_id}/approve", json={}, headers=ADMIN).status_code == 409
    events = [e["summary"] for e in client.get(f"/api/v1/tasks/{task['id']}", headers=ADMIN).json()["events"]]
    assert any("approved by admin" in e for e in events) and any("applied successfully" in e for e in events)


def test_change_blocked_on_protected_device_and_reject(client, rt):
    task = client.post("/api/v1/tasks", json={"title": "Add VLAN 50 named DMZ on fw-edge-01", "agent": "Fortinet Expert"}, headers=ADMIN).json()
    done = wait_task(client, task["id"])
    change_id = done["changes"][0]["id"]
    resp = client.post(f"/api/v1/changes/{change_id}/approve", json={}, headers=ADMIN)
    assert resp.status_code == 409 and "disabled for this device" in resp.json()["detail"]
    assert rt.db.get("change_requests", change_id)["status"] == "pending"
    assert client.post(f"/api/v1/changes/{change_id}/reject", json={"note": "wrong firewall"}, headers=ADMIN).status_code == 200
    change = rt.db.get("change_requests", change_id)
    assert change["status"] == "rejected" and change["decision_note"] == "wrong firewall" and change["output"] == ""


def test_console_is_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "NetAgent Console" in page.text
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/views.js").status_code == 200
