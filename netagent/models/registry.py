"""Builds provider instances from stored model definitions and tracks outages."""
from __future__ import annotations

import os
import time

from ..db import Database
from ..security import SecretBox
from .anthropic_provider import AnthropicProvider
from .base import ModelConfig, Provider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider

PROVIDERS = {
    "anthropic": AnthropicProvider,
    "ollama": OpenAICompatProvider,
    "openai": OpenAICompatProvider,
    "openai_compatible": OpenAICompatProvider,
    "azure_openai": OpenAICompatProvider,
    "mock": MockProvider,
}

PROVIDER_INFO = [
    {"id": "ollama", "name": "Ollama (local)", "location": "local", "needs_key": False,
     "base_url_hint": "http://localhost:11434"},
    {"id": "openai_compatible", "name": "OpenAI-compatible endpoint (LM Studio, vLLM, llama.cpp, gateways)",
     "location": "local", "needs_key": False, "base_url_hint": "http://localhost:1234/v1"},
    {"id": "anthropic", "name": "Anthropic Claude (cloud)", "location": "cloud", "needs_key": True,
     "base_url_hint": ""},
    {"id": "openai", "name": "OpenAI API (cloud)", "location": "cloud", "needs_key": True, "base_url_hint": ""},
    {"id": "azure_openai", "name": "Azure OpenAI (cloud)", "location": "cloud", "needs_key": True,
     "base_url_hint": "https://<resource>.openai.azure.com/openai/v1"},
    {"id": "mock", "name": "Mock (offline demo/testing)", "location": "local", "needs_key": False,
     "base_url_hint": ""},
]


class ModelRegistry:
    def __init__(self, db: Database, box: SecretBox):
        self.db = db
        self.box = box
        self._cache: dict[str, tuple[str, Provider]] = {}
        self._down: dict[str, tuple[float, str]] = {}

    def _config(self, row: dict) -> ModelConfig:
        key = ""
        if row.get("api_key_enc"):
            key = self.box.decrypt(row["api_key_enc"])
        if not key and row.get("api_key_env"):
            key = os.environ.get(row["api_key_env"], "")
        return ModelConfig(
            id=row["id"], name=row["name"], provider=row["provider"], model=row["model"],
            base_url=row.get("base_url") or "", api_key=key, location=row.get("location") or "cloud",
            enabled=bool(row.get("enabled", True)), cost_in=row.get("cost_in") or 0.0,
            cost_out=row.get("cost_out") or 0.0, options=row.get("options") or {},
        )

    def get(self, model_id: str) -> Provider | None:
        row = self.db.get("models", model_id)
        if not row:
            return None
        cfg = self._config(row)
        # Rebuild when the definition (or the resolved key) changes.
        fingerprint = repr((row, bool(cfg.api_key), cfg.api_key[-4:] if cfg.api_key else ""))
        cached = self._cache.get(model_id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        cls = PROVIDERS.get(cfg.provider)
        if cls is None:
            return None
        provider = cls(cfg)
        self._cache[model_id] = (fingerprint, provider)
        return provider

    # -- simple circuit breaker -----------------------------------------
    def mark_down(self, model_id: str, reason: str, ttl: float = 60.0) -> None:
        self._down[model_id] = (time.monotonic() + ttl, reason)

    def clear_down(self, model_id: str) -> None:
        self._down.pop(model_id, None)

    def down_reason(self, model_id: str) -> str | None:
        entry = self._down.get(model_id)
        if not entry:
            return None
        if entry[0] < time.monotonic():
            self._down.pop(model_id, None)
            return None
        return entry[1]

    def usable(self, model_id: str) -> tuple[Provider | None, str]:
        """Provider if this model can be tried right now, else (None, why not)."""
        provider = self.get(model_id)
        if provider is None:
            return None, "model is not defined"
        if not provider.cfg.enabled:
            return None, "disabled"
        ok, why = provider.available()
        if not ok:
            return None, why
        down = self.down_reason(model_id)
        if down:
            return None, f"temporarily skipped after a failure: {down}"
        return provider, "ok"
