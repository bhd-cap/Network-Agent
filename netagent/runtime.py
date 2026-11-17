"""The service container: one object that owns every long-lived component."""
from __future__ import annotations

import logging
import re

from .channels import ChannelManager
from .config import Settings, get_settings, load_yaml
from .db import Database, now
from .events import EventBus
from .models import ModelRegistry
from .orchestrator import Orchestrator
from .security import SecretBox

log = logging.getLogger("netagent")

DEMO_MODELS = [
    {"id": "demo-small", "name": "Demo small (mock, local)", "provider": "mock", "model": "mock-small",
     "location": "local", "options": {"capability": 2}},
    {"id": "demo-medium", "name": "Demo medium (mock, cloud)", "provider": "mock", "model": "mock-medium",
     "location": "cloud", "cost_in": 0.1, "cost_out": 0.5, "options": {"capability": 3}},
    {"id": "demo-large", "name": "Demo large (mock, cloud)", "provider": "mock", "model": "mock-large",
     "location": "cloud", "cost_in": 4.0, "cost_out": 20.0, "options": {"capability": 5}},
]
DEMO_LADDER = {
    "id": "demo", "name": "Demo ladder (offline mock models)",
    "description": "Three mock tiers so the platform can be explored with no model runtime or API key.",
    "tiers": [
        {"model": "demo-small", "max_complexity": 2, "min_confidence": 75},
        {"model": "demo-medium", "max_complexity": 4, "min_confidence": 70},
        {"model": "demo-large", "max_complexity": 5, "min_confidence": 0},
    ],
    "options": {"triage": True, "review": "high_risk", "high_risk_min_tier": 2},
}
DEMO_DEVICES = [
    {"name": "sw-core-01", "platform": "cisco_xe", "site": "HQ", "tags": "core,campus", "allow_config": True},
    {"name": "leaf-dc1-01", "platform": "arista_eos", "site": "DC1", "tags": "leaf,mlag", "allow_config": True},
    {"name": "fw-edge-01", "platform": "fortinet", "site": "HQ", "tags": "edge,firewall,vpn", "allow_config": False},
]
DEFAULT_AGENTS = [
    ("Network Lead", "network-lead"),
    ("Cisco Expert", "cisco-expert"),
    ("Fortinet Expert", "fortinet-expert"),
    ("Arista Expert", "arista-expert"),
]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "item"


class Runtime:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.db = Database(self.settings.db_path)
        self.box = SecretBox(self.settings.data_dir)
        self.bus = EventBus()
        self.registry = ModelRegistry(self.db, self.box)
        self.orchestrator = Orchestrator(self)
        self.channels = ChannelManager(self)
        self.seed()

    async def start(self) -> None:
        await self.orchestrator.start()
        await self.channels.start()

    async def stop(self) -> None:
        await self.channels.stop()
        await self.orchestrator.stop()
        self.db.close()

    async def notify_change_request(self, change_id: int) -> None:
        change = self.db.get("change_requests", change_id)
        if change:
            try:
                await self.channels.change_requested(change)
            except Exception:
                log.exception("change-request notification failed")

    # ------------------------------------------------------------------
    # first-run data
    # ------------------------------------------------------------------
    def seed(self) -> None:
        db = self.db
        config = load_yaml(self.settings.config_path) if self.settings.config_path.is_file() else {}

        if not db.scalar("SELECT COUNT(*) FROM models"):
            for m in config.get("models", []):
                self._insert_model(m)
        if not db.scalar("SELECT COUNT(*) FROM ladders"):
            for ladder in config.get("ladders", []):
                db.insert("ladders", {
                    "id": ladder["id"], "name": ladder.get("name", ladder["id"]),
                    "description": ladder.get("description", ""), "tiers": ladder.get("tiers", []),
                    "options": ladder.get("options", {}),
                })
            if config.get("default_ladder") and db.get_setting("default_ladder") is None:
                db.set_setting("default_ladder", config["default_ladder"])

        self.load_builtin_profiles()

        if self.settings.demo:
            for m in DEMO_MODELS:
                if not db.get("models", m["id"]):
                    self._insert_model(m)
            if not db.get("ladders", DEMO_LADDER["id"]):
                db.insert("ladders", DEMO_LADDER)
            if not db.get_setting("demo.seeded"):
                db.set_setting("default_ladder", DEMO_LADDER["id"])
                for d in DEMO_DEVICES:
                    if not db.one("SELECT id FROM devices WHERE name = ?", [d["name"]]):
                        db.insert("devices", {**d, "host": "", "transport": "simulated",
                                              "notes": "Simulated device for demo mode"})
                db.set_setting("demo.seeded", True)

        if not db.get_setting("agents.seeded"):
            for name, profile_id in DEFAULT_AGENTS:
                if db.get("profiles", profile_id) and not db.one("SELECT id FROM agents WHERE name = ?", [name]):
                    db.insert("agents", {"name": name, "profile_id": profile_id, "created_at": now()})
            db.set_setting("agents.seeded", True)

    def _insert_model(self, m: dict) -> None:
        self.db.insert("models", {
            "id": m["id"], "name": m.get("name", m["id"]), "provider": m["provider"], "model": m["model"],
            "base_url": m.get("base_url", ""), "api_key_env": m.get("api_key_env", ""),
            "location": m.get("location", "cloud"), "enabled": m.get("enabled", True),
            "cost_in": m.get("cost_in", 0), "cost_out": m.get("cost_out", 0), "options": m.get("options", {}),
        })

    def load_builtin_profiles(self) -> None:
        """Built-in profiles ship as YAML and are refreshed on every start."""
        folder = self.settings.profiles_dir
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.y*ml")):
            try:
                p = load_yaml(path)
                values = {
                    "name": p["name"], "description": p.get("description", ""), "personality": p.get("personality", ""),
                    "system_prompt": p.get("system_prompt", ""), "skills": p.get("skills", []),
                    "knowledge": p.get("knowledge", []), "keywords": p.get("keywords", []),
                    "ladder_id": p.get("ladder", ""), "coordinator": bool(p.get("coordinator", False)),
                    "builtin": True, "color": p.get("color", ""),
                }
                profile_id = p.get("id") or slugify(path.stem)
                if self.db.get("profiles", profile_id):
                    self.db.update("profiles", profile_id, values)
                else:
                    self.db.insert("profiles", {"id": profile_id, **values})
            except Exception as exc:
                log.error("could not load profile %s: %s", path.name, exc)
