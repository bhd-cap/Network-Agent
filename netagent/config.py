"""Runtime settings.

Everything is driven by environment variables (optionally loaded from a ``.env``
file) so the same build runs unchanged on Windows, Linux and in Docker.
"""
from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def expand_env(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references inside YAML values."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return expand_env(yaml.safe_load(fh) or {})


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    data_dir: Path
    host: str
    port: int
    admin_token: str
    demo: bool
    config_path: Path
    profiles_dir: Path
    knowledge_dir: Path
    public_url: str
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "netagent.db"


def _first_existing(*candidates: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return candidates[-1]


def build_settings() -> Settings:
    load_dotenv(Path(os.environ.get("NETAGENT_ENV_FILE", ".env")))

    data_dir = Path(os.environ.get("NETAGENT_DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    # Admin token: explicit env var, else generated once and persisted.
    token = os.environ.get("NETAGENT_ADMIN_TOKEN", "").strip()
    token_file = data_dir / "admin_token"
    if not token:
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
        else:
            token = secrets.token_urlsafe(24)
            token_file.write_text(token, encoding="utf-8")
            try:
                token_file.chmod(0o600)
            except OSError:
                pass  # Windows / exotic filesystems

    config_env = os.environ.get("NETAGENT_CONFIG")
    config_path = (
        Path(config_env)
        if config_env
        else _first_existing(Path("config/config.yaml").resolve(), PACKAGE_DIR / "defaults.yaml")
    )
    profiles_dir = Path(os.environ.get("NETAGENT_PROFILES_DIR", "")) if os.environ.get(
        "NETAGENT_PROFILES_DIR"
    ) else _first_existing(Path("profiles").resolve(), REPO_DIR / "profiles")
    knowledge_dir = Path(os.environ.get("NETAGENT_KNOWLEDGE_DIR", "")) if os.environ.get(
        "NETAGENT_KNOWLEDGE_DIR"
    ) else _first_existing(Path("knowledge").resolve(), REPO_DIR / "knowledge")

    port = int(os.environ.get("NETAGENT_PORT", "8080"))
    return Settings(
        data_dir=data_dir,
        host=os.environ.get("NETAGENT_HOST", "0.0.0.0"),
        port=port,
        admin_token=token,
        demo=_flag("NETAGENT_DEMO"),
        config_path=config_path,
        profiles_dir=profiles_dir,
        knowledge_dir=knowledge_dir,
        public_url=os.environ.get("NETAGENT_PUBLIC_URL", f"http://localhost:{port}").rstrip("/"),
        log_level=os.environ.get("NETAGENT_LOG_LEVEL", "INFO").upper(),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return build_settings()


def reset_settings() -> None:
    """Drop the cached settings (used by tests)."""
    get_settings.cache_clear()
