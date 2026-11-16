import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from netagent.config import build_settings  # noqa: E402
from netagent.db import now  # noqa: E402
from netagent.runtime import Runtime  # noqa: E402


@pytest.fixture
def make_rt(tmp_path, monkeypatch):
    """Factory for an isolated Runtime backed by a temp directory."""
    created = []

    def factory(demo: bool = False) -> Runtime:
        monkeypatch.setenv("NETAGENT_DATA_DIR", str(tmp_path / f"data{len(created)}"))
        monkeypatch.setenv("NETAGENT_ADMIN_TOKEN", "test-admin-token")
        monkeypatch.setenv("NETAGENT_ENV_FILE", str(tmp_path / "missing.env"))
        monkeypatch.setenv("NETAGENT_DEMO", "1" if demo else "0")
        monkeypatch.delenv("NETAGENT_CONFIG", raising=False)
        monkeypatch.delenv("NETAGENT_SECRET_KEY", raising=False)
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.chdir(ROOT)
        rt = Runtime(build_settings())
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        try:
            rt.db.close()
        except Exception:
            pass


@pytest.fixture
def rt(make_rt):
    return make_rt(demo=True)


def add_mock_model(rt: Runtime, model_id: str, **options) -> None:
    rt.db.insert("models", {
        "id": model_id, "name": model_id, "provider": "mock", "model": model_id,
        "location": options.pop("location", "local"), "cost_in": options.pop("cost_in", 0),
        "cost_out": options.pop("cost_out", 0), "options": options,
    })


def add_ladder(rt: Runtime, ladder_id: str, tiers: list[dict], **options) -> dict:
    rt.db.insert("ladders", {"id": ladder_id, "name": ladder_id, "tiers": tiers, "options": options})
    rt.db.set_setting("default_ladder", ladder_id)
    return rt.db.get("ladders", ladder_id)


def add_agent(rt: Runtime, name: str, profile_id: str) -> dict:
    agent_id = rt.db.insert("agents", {"name": name, "profile_id": profile_id, "created_at": now()})
    return rt.db.get("agents", agent_id)
