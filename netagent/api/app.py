"""HTTP surface: REST API, live event stream, channel webhooks and the web console."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__, reports
from ..channels import CHANNELS
from ..db import now
from ..models import PROVIDER_INFO
from ..orchestrator import PRIORITIES, TERMINAL
from ..runtime import Runtime, slugify
from ..security import constant_time_equals, hash_token, new_api_key
from ..tools import SKILLS, tools_for
from ..tools.devices import PLATFORMS, apply_change, test_device

log = logging.getLogger("netagent.api")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
COOKIE = "netagent_session"


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------
class LoginBody(BaseModel):
    token: str


class TaskBody(BaseModel):
    title: str = ""
    description: str = ""
    agent: str | int | None = None
    priority: int = 3
    requester: str = ""
    callback_url: str = ""
    thread_key: str = ""


class AskBody(BaseModel):
    question: str
    agent: str | int | None = None
    timeout: float = Field(default=120, ge=1, le=900)
    thread_key: str = ""


class AgentBody(BaseModel):
    name: str | None = None
    profile_id: str | None = None
    ladder_id: str | None = None
    status: str | None = None
    max_parallel: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = None


class ProfileBody(BaseModel):
    name: str
    description: str = ""
    personality: str = ""
    system_prompt: str = ""
    skills: list[str] = []
    knowledge: list[str] = []
    keywords: list[str] = []
    ladder_id: str = ""
    coordinator: bool = False
    color: str = ""


class ModelBody(BaseModel):
    id: str | None = None
    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    api_key: str | None = None  # None = keep, "" = clear
    location: str = "cloud"
    enabled: bool = True
    cost_in: float = 0
    cost_out: float = 0
    options: dict = {}


class LadderBody(BaseModel):
    id: str | None = None
    name: str
    description: str = ""
    tiers: list[dict]
    options: dict = {}


class DeviceBody(BaseModel):
    name: str
    host: str = ""
    port: int = 22
    platform: str
    transport: str = "ssh"
    username: str = ""
    password: str | None = None
    enable_password: str | None = None
    site: str = ""
    tags: str = ""
    allow_config: bool = False
    notes: str = ""


class DecisionBody(BaseModel):
    note: str = ""
    save_config: bool = False


class KeyBody(BaseModel):
    name: str
    scope: str = "tasks"


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
@dataclass
class Principal:
    name: str
    scope: str  # admin | tasks
    via: str  # token | key | cookie


def get_rt(request: Request) -> Runtime:
    return request.app.state.rt


def authenticate(request: Request) -> Principal | None:
    rt: Runtime = request.app.state.rt
    token = ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    token = token or request.headers.get("x-api-key", "").strip()
    if token:
        if constant_time_equals(token, rt.settings.admin_token):
            return Principal("admin", "admin", "token")
        row = rt.db.one("SELECT * FROM api_keys WHERE key_hash = ? AND revoked = 0", [hash_token(token)])
        if row:
            rt.db.update("api_keys", row["id"], {"last_used_at": now()})
            return Principal(f"key:{row['name']}", row["scope"], "key")
        return None
    cookie = request.cookies.get(COOKIE)
    if cookie and rt.box.verify_session(cookie):
        # Cookie sessions must send a custom header on writes: browsers will not add it cross-site (CSRF).
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("x-requested-with") != "netagent":
            return None
        return Principal("admin", "admin", "cookie")
    return None


def require_auth(request: Request) -> Principal:
    principal = authenticate(request)
    if principal is None:
        raise HTTPException(401, "Authentication required")
    return principal


def require_admin(principal: Principal = Depends(require_auth)) -> Principal:
    if principal.scope != "admin":
        raise HTTPException(403, "This API key is limited to task operations")
    return principal


# ---------------------------------------------------------------------------
# serialisers
# ---------------------------------------------------------------------------
def _task_row(rt: Runtime, task: dict, brief: bool = False) -> dict:
    out = dict(task)
    agent = rt.db.get("agents", task["agent_id"]) if task["agent_id"] else None
    model = rt.db.get("models", task["model_id"]) if task["model_id"] else None
    out["agent_name"] = agent["name"] if agent else None
    out["model_name"] = model["name"] if model else None
    out["model_location"] = model["location"] if model else None
    out["seconds"] = reports._seconds(task["started_at"], task["completed_at"])
    out["children"] = rt.db.scalar("SELECT COUNT(*) FROM tasks WHERE parent_id = ?", [task["id"]])
    ref = dict(out.get("source_ref") or {})
    out["source_ref"] = {k: ref[k] for k in ("mode", "from", "subject", "callback_url", "inline") if k in ref}
    if brief:
        out["result"] = (out["result"] or "")[:300]
        out["description"] = (out["description"] or "")[:300]
    return out


def _model_row(rt: Runtime, row: dict) -> dict:
    out = {k: v for k, v in row.items() if k != "api_key_enc"}
    out["has_key"] = bool(row.get("api_key_enc"))
    provider, why = rt.registry.usable(row["id"])
    out["usable"] = provider is not None
    out["status"] = why
    return out


def _device_row(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in {"password_enc", "enable_enc"}}
    out["has_password"] = bool(row.get("password_enc"))
    out["has_enable"] = bool(row.get("enable_enc"))
    out["platform_name"] = PLATFORMS.get(row["platform"], row["platform"])
    return out


def _resolve_agent(rt: Runtime, ref: str | int | None) -> int | None:
    if ref in (None, "", 0):
        return None
    row = None
    if isinstance(ref, int) or str(ref).isdigit():
        row = rt.db.get("agents", int(ref))
    if not row:
        row = rt.db.one("SELECT * FROM agents WHERE name = ? COLLATE NOCASE", [str(ref)])
    if not row:
        raise HTTPException(404, f"No agent '{ref}'")
    return row["id"]


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_app(rt: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.rt = rt or Runtime()
        await app.state.rt.start()
        try:
            yield
        finally:
            await app.state.rt.stop()

    app = FastAPI(title="NetAgent", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    failed_logins: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ auth
    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.post("/api/v1/auth/login")
    async def login(body: LoginBody, request: Request, response: Response):
        rt = get_rt(request)
        ip = request.client.host if request.client else "?"
        recent = [t for t in failed_logins.get(ip, []) if t > time.time() - 300]
        if len(recent) >= 10:
            raise HTTPException(429, "Too many failed sign-in attempts. Try again in a few minutes.")
        if not constant_time_equals(body.token.strip(), rt.settings.admin_token):
            failed_logins[ip] = recent + [time.time()]
            raise HTTPException(401, "Invalid admin token")
        failed_logins.pop(ip, None)
        response.set_cookie(COOKIE, rt.box.sign_session("admin"), httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", max_age=12 * 3600, path="/")
        return {"ok": True}

    @app.post("/api/v1/auth/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/v1/auth/me")
    async def me(request: Request, principal: Principal = Depends(require_auth)):
        rt = get_rt(request)
        return {"name": principal.name, "scope": principal.scope, "version": __version__, "demo": rt.settings.demo,
                "public_url": rt.settings.public_url}

    # ------------------------------------------------------------- overview
    @app.get("/api/v1/overview")
    async def overview(request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        db = rt.db
        counts = {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
        day = reports._since(1)
        agents = []
        for a in db.query("SELECT a.*, p.name AS profile_name, p.color AS color, p.coordinator AS coordinator "
                          "FROM agents a LEFT JOIN profiles p ON p.id = a.profile_id ORDER BY a.id"):
            current = db.one("SELECT id, title FROM tasks WHERE agent_id = ? AND status = 'running' ORDER BY started_at LIMIT 1", [a["id"]])
            agents.append({
                **a, "current_task": current,
                "queued": db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status = 'queued'", [a["id"]]),
                "running": db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status = 'running'", [a["id"]]),
                "completed": db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status = 'completed'", [a["id"]]),
            })
        activity = db.query(
            "SELECT e.id, e.task_id, e.ts, e.kind, e.summary, t.title FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "ORDER BY e.id DESC LIMIT 30"
        )
        return {
            "counts": {
                "queued": counts.get("queued", 0), "running": counts.get("running", 0),
                "completed": counts.get("completed", 0), "failed": counts.get("failed", 0),
                "completed_24h": db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'completed' AND completed_at >= ?", [day]),
                "failed_24h": db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'failed' AND completed_at >= ?", [day]),
                "needs_review": db.scalar("SELECT COUNT(*) FROM tasks WHERE status = 'completed' AND needs_review = 1 AND completed_at >= ?", [reports._since(7)]),
                "pending_changes": db.scalar("SELECT COUNT(*) FROM change_requests WHERE status = 'pending'"),
            },
            "agents": agents,
            "activity": activity,
            "ladder": db.get("ladders", db.get_setting("default_ladder") or "") or None,
            "models_usable": sum(1 for m in db.query("SELECT id FROM models WHERE enabled = 1") if rt.registry.usable(m["id"])[0]),
        }

    # ---------------------------------------------------------------- tasks
    @app.get("/api/v1/tasks")
    async def list_tasks(request: Request, status: str = "", agent_id: int | None = None, source: str = "",
                         q: str = "", parent_id: int | None = None, include_children: bool = False,
                         thread_key: str = "", full: bool = False, limit: int = 100,
                         _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        where, params = ["1=1"], []
        if status == "open":
            where.append("status IN ('queued','running')")
        elif status == "review":
            where.append("status = 'completed' AND needs_review = 1")
        elif status:
            where.append("status = ?")
            params.append(status)
        if agent_id:
            where.append("agent_id = ?")
            params.append(agent_id)
        if source:
            where.append("source = ?")
            params.append(source)
        if q:
            where.append("(title LIKE ? OR description LIKE ? OR requester LIKE ?)")
            params += [f"%{q}%"] * 3
        if thread_key:
            where.append("thread_key = ?")
            params.append(thread_key)
        if parent_id is not None:
            where.append("parent_id = ?")
            params.append(parent_id)
        elif not include_children:
            where.append("parent_id IS NULL")
        rows = rt.db.query(
            f"SELECT * FROM tasks WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", params + [max(1, min(limit, 500))]
        )
        return [_task_row(rt, r, brief=not full) for r in rows]

    def _new_task(rt: Runtime, body: TaskBody, principal: Principal, source: str) -> dict:
        if not body.title.strip() and not body.description.strip():
            raise HTTPException(422, "A task needs a title or a description")
        ref: dict[str, Any] = {}
        if body.callback_url:
            if not body.callback_url.lower().startswith(("http://", "https://")):
                raise HTTPException(422, "callback_url must be an http(s) URL")
            ref["callback_url"] = body.callback_url
        return rt.orchestrator.create_task(
            title=body.title, description=body.description, agent_id=_resolve_agent(rt, body.agent), source=source,
            source_ref=ref, requester=body.requester or principal.name, priority=body.priority,
            thread_key=body.thread_key,
        )

    @app.post("/api/v1/tasks", status_code=201)
    async def create_task(body: TaskBody, request: Request, principal: Principal = Depends(require_auth)):
        rt = get_rt(request)
        task = _new_task(rt, body, principal, "web" if principal.via == "cookie" else "api")
        return _task_row(rt, task)

    @app.post("/api/v1/ask")
    async def ask(body: AskBody, request: Request, principal: Principal = Depends(require_auth)):
        """Submit a question and wait for the answer (synchronous convenience endpoint)."""
        rt = get_rt(request)
        task = _new_task(rt, TaskBody(description=body.question, agent=body.agent, thread_key=body.thread_key),
                         principal, "web" if principal.via == "cookie" else "api")
        done = await rt.orchestrator.wait_for(task["id"], body.timeout)
        out = _task_row(rt, done)
        return JSONResponse(out, status_code=200 if done["status"] in TERMINAL else 202)

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(task_id: int, request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        task = rt.db.get("tasks", task_id)
        if not task:
            raise HTTPException(404, "No such task")
        out = _task_row(rt, task)
        out["events"] = rt.db.query("SELECT * FROM task_events WHERE task_id = ? ORDER BY id", [task_id])
        out["subtasks"] = [_task_row(rt, r, brief=True) for r in rt.db.query("SELECT * FROM tasks WHERE parent_id = ? ORDER BY id", [task_id])]
        out["changes"] = rt.db.query(
            "SELECT c.id, c.summary, c.status, c.risk, d.name AS device FROM change_requests c "
            "LEFT JOIN devices d ON d.id = c.device_id WHERE c.task_id = ? ORDER BY c.id", [task_id])
        if task["parent_id"]:
            parent = rt.db.get("tasks", task["parent_id"])
            out["parent"] = {"id": parent["id"], "title": parent["title"]} if parent else None
        return out

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: int, request: Request, _: Principal = Depends(require_auth)):
        if not get_rt(request).orchestrator.cancel(task_id):
            raise HTTPException(409, "The task is not open")
        return {"ok": True}

    @app.post("/api/v1/tasks/{task_id}/retry")
    async def retry_task(task_id: int, request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        task = rt.orchestrator.retry(task_id)
        if not task:
            raise HTTPException(409, "Only finished tasks can be re-run")
        return _task_row(rt, task)

    @app.get("/api/v1/tasks/{task_id}/report")
    async def get_task_report(task_id: int, request: Request, _: Principal = Depends(require_auth)):
        text = reports.task_report(get_rt(request), task_id)
        if text is None:
            raise HTTPException(404, "No such task")
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    # --------------------------------------------------------------- agents
    @app.get("/api/v1/agents")
    async def list_agents(request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        out = []
        for a in rt.db.query("SELECT * FROM agents ORDER BY id"):
            profile = rt.db.get("profiles", a["profile_id"]) or {}
            ladder = rt.orchestrator.resolve_ladder(a, profile) if profile else None
            out.append({
                **a, "profile_name": profile.get("name"), "profile_description": profile.get("description"),
                "color": profile.get("color"), "coordinator": profile.get("coordinator"),
                "skills": profile.get("skills", []), "effective_ladder": ladder["name"] if ladder else None,
                "open": rt.db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status IN ('queued','running')", [a["id"]]),
                "completed": rt.db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status = 'completed'", [a["id"]]),
                "failed": rt.db.scalar("SELECT COUNT(*) FROM tasks WHERE agent_id = ? AND status = 'failed'", [a["id"]]),
            })
        return out

    @app.post("/api/v1/agents", status_code=201)
    async def create_agent(body: AgentBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        name = (body.name or "").strip()
        if not name or not body.profile_id:
            raise HTTPException(422, "name and profile_id are required")
        if not rt.db.get("profiles", body.profile_id):
            raise HTTPException(404, "No such profile")
        if rt.db.one("SELECT id FROM agents WHERE name = ? COLLATE NOCASE", [name]):
            raise HTTPException(409, f"An agent named '{name}' already exists")
        if body.ladder_id and not rt.db.get("ladders", body.ladder_id):
            raise HTTPException(404, "No such ladder")
        agent_id = rt.db.insert("agents", {
            "name": name, "profile_id": body.profile_id, "ladder_id": body.ladder_id or "",
            "max_parallel": body.max_parallel or 2, "notes": body.notes or "", "created_at": now(),
        })
        rt.bus.publish("agent.updated", agent_id=agent_id)
        rt.orchestrator.kick()
        return rt.db.get("agents", agent_id)

    @app.patch("/api/v1/agents/{agent_id}")
    async def update_agent(agent_id: int, body: AgentBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if not rt.db.get("agents", agent_id):
            raise HTTPException(404, "No such agent")
        values = body.model_dump(exclude_none=True)
        if "status" in values and values["status"] not in {"active", "paused"}:
            raise HTTPException(422, "status must be active or paused")
        if "profile_id" in values and not rt.db.get("profiles", values["profile_id"]):
            raise HTTPException(404, "No such profile")
        if values.get("ladder_id") and not rt.db.get("ladders", values["ladder_id"]):
            raise HTTPException(404, "No such ladder")
        if "name" in values:
            values["name"] = values["name"].strip()
            clash = rt.db.one("SELECT id FROM agents WHERE name = ? COLLATE NOCASE AND id != ?", [values["name"], agent_id])
            if clash or not values["name"]:
                raise HTTPException(409, "That name is empty or already in use")
        rt.db.update("agents", agent_id, values)
        rt.bus.publish("agent.updated", agent_id=agent_id)
        rt.orchestrator.kick()
        return rt.db.get("agents", agent_id)

    @app.delete("/api/v1/agents/{agent_id}")
    async def delete_agent(agent_id: int, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if not rt.db.get("agents", agent_id):
            raise HTTPException(404, "No such agent")
        for task in rt.db.query("SELECT id FROM tasks WHERE agent_id = ? AND status = 'running'", [agent_id]):
            rt.orchestrator.cancel(task["id"])
        # Waiting work is handed back to the router rather than lost.
        rt.db.execute("UPDATE tasks SET agent_id = NULL WHERE agent_id = ? AND status = 'queued'", [agent_id])
        rt.db.delete("agents", agent_id)
        rt.bus.publish("agent.updated", agent_id=agent_id)
        rt.orchestrator.kick()
        return {"ok": True}

    # ------------------------------------------------------------- profiles
    @app.get("/api/v1/profiles")
    async def list_profiles(request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        out = []
        for p in rt.db.query("SELECT * FROM profiles ORDER BY builtin DESC, name"):
            p["agents"] = rt.db.scalar("SELECT COUNT(*) FROM agents WHERE profile_id = ?", [p["id"]])
            p["tools"] = [t.spec.name for t in tools_for(p["skills"])]
            out.append(p)
        return out

    @app.get("/api/v1/skills")
    async def list_skills(request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        folder = rt.settings.knowledge_dir
        packs = sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
        return {
            "skills": [{"id": k, **v, "tools": [t.spec.name for t in tools_for([k]) if t.skill == k]} for k, v in SKILLS.items()],
            "knowledge": [{"id": name, "playbooks": sorted(f.stem for f in (folder / name).glob("*.md"))} for name in packs],
        }

    def _profile_values(rt: Runtime, body: ProfileBody) -> dict:
        unknown = [s for s in body.skills if s not in SKILLS]
        if unknown:
            raise HTTPException(422, f"Unknown skill(s): {', '.join(unknown)}")
        if body.ladder_id and not rt.db.get("ladders", body.ladder_id):
            raise HTTPException(404, "No such ladder")
        values = body.model_dump()
        values["keywords"] = [k.strip().lower() for k in body.keywords if k.strip()]
        return values

    @app.post("/api/v1/profiles", status_code=201)
    async def create_profile(body: ProfileBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        base = slugify(body.name)
        profile_id, n = base, 2
        while rt.db.get("profiles", profile_id):
            profile_id, n = f"{base}-{n}", n + 1
        rt.db.insert("profiles", {"id": profile_id, **_profile_values(rt, body), "builtin": False})
        return rt.db.get("profiles", profile_id)

    @app.put("/api/v1/profiles/{profile_id}")
    async def update_profile(profile_id: str, body: ProfileBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        profile = rt.db.get("profiles", profile_id)
        if not profile:
            raise HTTPException(404, "No such profile")
        if profile["builtin"]:
            raise HTTPException(409, "Built-in profiles are read-only. Duplicate it to make changes.")
        rt.db.update("profiles", profile_id, _profile_values(rt, body))
        return rt.db.get("profiles", profile_id)

    @app.delete("/api/v1/profiles/{profile_id}")
    async def delete_profile(profile_id: str, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        profile = rt.db.get("profiles", profile_id)
        if not profile:
            raise HTTPException(404, "No such profile")
        if profile["builtin"]:
            raise HTTPException(409, "Built-in profiles cannot be deleted")
        if rt.db.scalar("SELECT COUNT(*) FROM agents WHERE profile_id = ?", [profile_id]):
            raise HTTPException(409, "Agents still use this profile")
        rt.db.delete("profiles", profile_id)
        return {"ok": True}

    # ------------------------------------------------------- models/ladders
    @app.get("/api/v1/models")
    async def list_models(request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        return {"models": [_model_row(rt, m) for m in rt.db.query("SELECT * FROM models ORDER BY location DESC, name")],
                "providers": PROVIDER_INFO}

    def _model_values(rt: Runtime, body: ModelBody) -> dict:
        if body.provider not in {p["id"] for p in PROVIDER_INFO}:
            raise HTTPException(422, f"Unknown provider '{body.provider}'")
        values = body.model_dump(exclude={"id", "api_key"})
        values["location"] = "local" if body.location == "local" else "cloud"
        if body.api_key is not None:
            values["api_key_enc"] = rt.box.encrypt(body.api_key.strip())
        return values

    @app.post("/api/v1/models", status_code=201)
    async def create_model(body: ModelBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        model_id = slugify(body.id or body.name)
        if rt.db.get("models", model_id):
            raise HTTPException(409, f"A model with id '{model_id}' already exists")
        rt.db.insert("models", {"id": model_id, **_model_values(rt, body)})
        return _model_row(rt, rt.db.get("models", model_id))

    @app.put("/api/v1/models/{model_id}")
    async def update_model(model_id: str, body: ModelBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if not rt.db.get("models", model_id):
            raise HTTPException(404, "No such model")
        rt.db.update("models", model_id, _model_values(rt, body))
        rt.registry.clear_down(model_id)
        return _model_row(rt, rt.db.get("models", model_id))

    @app.delete("/api/v1/models/{model_id}")
    async def delete_model(model_id: str, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        used = [l["name"] for l in rt.db.query("SELECT * FROM ladders") if any(t.get("model") == model_id for t in l["tiers"])]
        if used:
            raise HTTPException(409, f"Model is used by ladder(s): {', '.join(used)}")
        rt.db.delete("models", model_id)
        return {"ok": True}

    @app.post("/api/v1/models/{model_id}/test")
    async def test_model(model_id: str, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        provider = rt.registry.get(model_id)
        if not provider:
            raise HTTPException(404, "No such model")
        rt.registry.clear_down(model_id)
        ok, message = await provider.health()
        return {"ok": ok, "message": message}

    @app.get("/api/v1/ladders")
    async def list_ladders(request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        default = rt.db.get_setting("default_ladder")
        out = []
        for ladder in rt.db.query("SELECT * FROM ladders ORDER BY name"):
            for tier in ladder["tiers"]:
                model = rt.db.get("models", tier.get("model", ""))
                provider, why = rt.registry.usable(tier.get("model", ""))
                tier.update(model_name=model["name"] if model else tier.get("model"),
                            location=model["location"] if model else "", usable=provider is not None, status=why)
            ladder["default"] = ladder["id"] == default
            out.append(ladder)
        return out

    def _ladder_values(rt: Runtime, body: LadderBody) -> dict:
        if not body.tiers:
            raise HTTPException(422, "A ladder needs at least one tier")
        tiers = []
        for tier in body.tiers:
            if not rt.db.get("models", str(tier.get("model", ""))):
                raise HTTPException(422, f"Tier refers to unknown model '{tier.get('model')}'")
            tiers.append({
                "model": tier["model"],
                "max_complexity": max(1, min(5, int(tier.get("max_complexity", 5)))),
                "min_confidence": max(0, min(100, int(tier.get("min_confidence", 70)))),
            })
        options = {
            "triage": bool(body.options.get("triage", True)),
            "review": body.options.get("review", "high_risk") if body.options.get("review") in {"never", "high_risk", "always"} else "high_risk",
            "high_risk_min_tier": max(1, min(len(tiers), int(body.options.get("high_risk_min_tier", 1)))),
        }
        return {"name": body.name, "description": body.description, "tiers": tiers, "options": options}

    @app.post("/api/v1/ladders", status_code=201)
    async def create_ladder(body: LadderBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        ladder_id = slugify(body.id or body.name)
        if rt.db.get("ladders", ladder_id):
            raise HTTPException(409, f"A ladder with id '{ladder_id}' already exists")
        rt.db.insert("ladders", {"id": ladder_id, **_ladder_values(rt, body)})
        return rt.db.get("ladders", ladder_id)

    @app.put("/api/v1/ladders/{ladder_id}")
    async def update_ladder(ladder_id: str, body: LadderBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if not rt.db.get("ladders", ladder_id):
            raise HTTPException(404, "No such ladder")
        rt.db.update("ladders", ladder_id, _ladder_values(rt, body))
        return rt.db.get("ladders", ladder_id)

    @app.delete("/api/v1/ladders/{ladder_id}")
    async def delete_ladder(ladder_id: str, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if rt.db.get_setting("default_ladder") == ladder_id:
            raise HTTPException(409, "This is the default ladder. Choose another default first.")
        rt.db.delete("ladders", ladder_id)
        rt.db.execute("UPDATE agents SET ladder_id = '' WHERE ladder_id = ?", [ladder_id])
        return {"ok": True}

    # -------------------------------------------------------------- devices
    @app.get("/api/v1/devices")
    async def list_devices(request: Request, _: Principal = Depends(require_admin)):
        return {"devices": [_device_row(d) for d in get_rt(request).db.query("SELECT * FROM devices ORDER BY name")],
                "platforms": PLATFORMS}

    def _device_values(rt: Runtime, body: DeviceBody) -> dict:
        if body.platform not in PLATFORMS:
            raise HTTPException(422, f"Unknown platform '{body.platform}'")
        if body.transport not in {"ssh", "simulated"}:
            raise HTTPException(422, "transport must be ssh or simulated")
        values = body.model_dump(exclude={"password", "enable_password"})
        values["name"] = body.name.strip()
        if not values["name"]:
            raise HTTPException(422, "name is required")
        if body.password is not None:
            values["password_enc"] = rt.box.encrypt(body.password)
        if body.enable_password is not None:
            values["enable_enc"] = rt.box.encrypt(body.enable_password)
        return values

    @app.post("/api/v1/devices", status_code=201)
    async def create_device(body: DeviceBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        values = _device_values(rt, body)
        if rt.db.one("SELECT id FROM devices WHERE name = ? COLLATE NOCASE", [values["name"]]):
            raise HTTPException(409, "A device with that name already exists")
        return _device_row(rt.db.get("devices", rt.db.insert("devices", values)))

    @app.put("/api/v1/devices/{device_id}")
    async def update_device(device_id: int, body: DeviceBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if not rt.db.get("devices", device_id):
            raise HTTPException(404, "No such device")
        values = _device_values(rt, body)
        if rt.db.one("SELECT id FROM devices WHERE name = ? COLLATE NOCASE AND id != ?", [values["name"], device_id]):
            raise HTTPException(409, "A device with that name already exists")
        rt.db.update("devices", device_id, values)
        return _device_row(rt.db.get("devices", device_id))

    @app.delete("/api/v1/devices/{device_id}")
    async def delete_device(device_id: int, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if rt.db.scalar("SELECT COUNT(*) FROM change_requests WHERE device_id = ? AND status IN ('pending','approved')", [device_id]):
            raise HTTPException(409, "The device has open change requests")
        rt.db.delete("devices", device_id)
        return {"ok": True}

    @app.post("/api/v1/devices/{device_id}/test")
    async def test_device_route(device_id: int, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        device = rt.db.get("devices", device_id)
        if not device:
            raise HTTPException(404, "No such device")
        ok, message = await test_device(rt, device)
        return {"ok": ok, "message": message}

    # ------------------------------------------------------ change requests
    def _change_row(rt: Runtime, c: dict) -> dict:
        device = rt.db.get("devices", c["device_id"])
        agent = rt.db.get("agents", c["agent_id"]) if c["agent_id"] else None
        task = rt.db.get("tasks", c["task_id"]) if c["task_id"] else None
        return {**c, "device": device["name"] if device else "(deleted)",
                "platform": PLATFORMS.get(device["platform"], "") if device else "",
                "allow_config": bool(device and device["allow_config"]),
                "agent": agent["name"] if agent else None, "task_title": task["title"] if task else None}

    @app.get("/api/v1/changes")
    async def list_changes(request: Request, status: str = "", _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        rows = rt.db.query(
            "SELECT * FROM change_requests " + ("WHERE status = ? " if status else "") + "ORDER BY id DESC LIMIT 200",
            [status] if status else [],
        )
        return [_change_row(rt, c) for c in rows]

    async def _apply(rt: Runtime, change_id: int, save: bool) -> None:
        change = rt.db.get("change_requests", change_id)
        ok, output, backup = await apply_change(rt, change, save)
        rt.db.update("change_requests", change_id, {"status": "applied" if ok else "failed", "output": output, "backup": backup})
        if change["task_id"]:
            rt.orchestrator.add_event(
                change["task_id"], "approval",
                f"CR-{change_id} {'applied successfully' if ok else 'FAILED to apply: ' + output[:300]}", {"change_id": change_id},
            )
        rt.bus.publish("change.updated", change_id=change_id)

    @app.post("/api/v1/changes/{change_id}/approve")
    async def approve_change(change_id: int, body: DecisionBody, request: Request, principal: Principal = Depends(require_admin)):
        rt = get_rt(request)
        change = rt.db.get("change_requests", change_id)
        if not change or change["status"] != "pending":
            raise HTTPException(409, "The change request is not pending")
        device = rt.db.get("devices", change["device_id"])
        if not device or not device["allow_config"]:
            raise HTTPException(409, "Configuration pushes are disabled for this device. Enable 'allow configuration "
                                     "changes' on the device first, or reject and apply manually.")
        rt.db.update("change_requests", change_id, {
            "status": "approved", "decided_by": principal.name, "decided_at": now(), "decision_note": body.note,
        })
        if change["task_id"]:
            rt.orchestrator.add_event(change["task_id"], "approval", f"CR-{change_id} approved by {principal.name}; pushing to {device['name']}")
        rt.bus.publish("change.updated", change_id=change_id)
        asyncio.create_task(_apply(rt, change_id, body.save_config))
        return {"ok": True, "status": "approved"}

    @app.post("/api/v1/changes/{change_id}/reject")
    async def reject_change(change_id: int, body: DecisionBody, request: Request, principal: Principal = Depends(require_admin)):
        rt = get_rt(request)
        change = rt.db.get("change_requests", change_id)
        if not change or change["status"] != "pending":
            raise HTTPException(409, "The change request is not pending")
        rt.db.update("change_requests", change_id, {
            "status": "rejected", "decided_by": principal.name, "decided_at": now(), "decision_note": body.note,
        })
        if change["task_id"]:
            rt.orchestrator.add_event(change["task_id"], "approval",
                                      f"CR-{change_id} rejected by {principal.name}" + (f": {body.note}" if body.note else ""))
        rt.bus.publish("change.updated", change_id=change_id)
        return {"ok": True, "status": "rejected"}

    # ------------------------------------------------------------- channels
    @app.get("/api/v1/channels")
    async def get_channels(request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        status = rt.channels.status()
        return [{"id": name, "title": spec["title"], "schema": spec["schema"], "values": rt.channels.public_config(name),
                 "status": status[name]} for name, spec in CHANNELS.items()]

    @app.put("/api/v1/channels/{name}")
    async def put_channel(name: str, values: dict, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if name not in CHANNELS:
            raise HTTPException(404, "No such channel")
        rt.channels.set_config(name, values)
        return {"ok": True, "values": rt.channels.public_config(name)}

    @app.post("/api/v1/channels/{name}/test")
    async def test_channel(name: str, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if name not in CHANNELS:
            raise HTTPException(404, "No such channel")
        ok, message = await rt.channels.test(name)
        return {"ok": ok, "message": message}

    # ------------------------------------------------------------- api keys
    @app.get("/api/v1/keys")
    async def list_keys(request: Request, _: Principal = Depends(require_admin)):
        return get_rt(request).db.query(
            "SELECT id, name, prefix, scope, created_at, last_used_at FROM api_keys WHERE revoked = 0 ORDER BY id")

    @app.post("/api/v1/keys", status_code=201)
    async def create_key(body: KeyBody, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        if body.scope not in {"tasks", "admin"}:
            raise HTTPException(422, "scope must be tasks or admin")
        key, prefix, digest = new_api_key()
        key_id = rt.db.insert("api_keys", {"name": body.name.strip() or "unnamed", "prefix": prefix, "key_hash": digest,
                                          "scope": body.scope, "created_at": now()})
        return {"id": key_id, "key": key, "prefix": prefix, "scope": body.scope}

    @app.delete("/api/v1/keys/{key_id}")
    async def revoke_key(key_id: int, request: Request, _: Principal = Depends(require_admin)):
        get_rt(request).db.update("api_keys", key_id, {"revoked": True})
        return {"ok": True}

    # ----------------------------------------------------- reports/settings
    @app.get("/api/v1/reports/summary")
    async def report_summary(request: Request, days: int = 7, _: Principal = Depends(require_auth)):
        return reports.summary(get_rt(request), max(1, min(days, 365)))

    @app.get("/api/v1/reports/tasks.csv")
    async def report_csv(request: Request, days: int = 30, _: Principal = Depends(require_auth)):
        return Response(reports.tasks_csv(get_rt(request), max(1, min(days, 365))), media_type="text/csv",
                        headers={"Content-Disposition": "attachment; filename=netagent-tasks.csv"})

    @app.get("/api/v1/reports/digest")
    async def report_digest(request: Request, days: int = 1, _: Principal = Depends(require_auth)):
        return PlainTextResponse(reports.digest_markdown(get_rt(request), max(1, min(days, 365))),
                                 media_type="text/markdown; charset=utf-8")

    @app.post("/api/v1/reports/send")
    async def report_send(request: Request, days: int = 1, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        cfg = rt.channels.get_config("notifications")
        if not (cfg.get("teams_webhook_url") or cfg.get("email_recipients")):
            raise HTTPException(409, "Configure a Teams webhook or email recipients under Integrations > Notifications first")
        problems = await rt.channels.send_digest(max(1, min(days, 365)))
        return {"ok": not problems, "message": "; ".join(problems) or "Report sent"}

    SETTINGS = {"default_ladder": str, "redact_secrets": bool, "max_collab_depth": int, "max_steps": int}

    @app.get("/api/v1/settings")
    async def get_settings_route(request: Request, _: Principal = Depends(require_admin)):
        db = get_rt(request).db
        return {
            "default_ladder": db.get_setting("default_ladder", ""), "redact_secrets": db.get_setting("redact_secrets", True),
            "max_collab_depth": db.get_setting("max_collab_depth", 3), "max_steps": db.get_setting("max_steps", 12),
        }

    @app.put("/api/v1/settings")
    async def put_settings_route(values: dict, request: Request, _: Principal = Depends(require_admin)):
        rt = get_rt(request)
        for key, caster in SETTINGS.items():
            if key in values:
                value = caster(values[key])
                if key == "default_ladder" and not rt.db.get("ladders", value):
                    raise HTTPException(404, "No such ladder")
                if key == "max_collab_depth":
                    value = max(1, min(6, value))
                if key == "max_steps":
                    value = max(2, min(40, value))
                rt.db.set_setting(key, value)
        return await get_settings_route(request)

    # --------------------------------------------------------- live events
    @app.get("/api/v1/events")
    async def events(request: Request, _: Principal = Depends(require_auth)):
        rt = get_rt(request)
        queue = rt.bus.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                rt.bus.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------ channel webhooks
    @app.post("/hooks/teams/messages")
    async def teams_bot(request: Request):
        try:
            activity = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        status, body = await get_rt(request).channels.teams.handle_bot(request.headers.get("authorization", ""), activity)
        return JSONResponse(body, status_code=status)

    @app.post("/hooks/teams/outgoing")
    async def teams_outgoing(request: Request):
        raw = await request.body()
        try:
            activity = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        status, body = await get_rt(request).channels.teams.handle_outgoing(request.headers.get("authorization", ""), raw, activity)
        return JSONResponse(body, status_code=status)

    # ------------------------------------------------------------ web console
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app
