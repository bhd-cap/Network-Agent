"""Provider-neutral chat types.

Every model backend (Claude, Ollama, any OpenAI-compatible endpoint, the offline
mock) is adapted to the same small interface so the escalation ladder can move a
task between local and cloud models without caring which is which.

Conversation history is a list of plain dicts:

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [ToolCall...],
     "raw": <provider payload>, "raw_provider": "anthropic"}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "...", "is_error": False}
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end"  # end | tool_use | max_tokens | refusal | pause
    input_tokens: int = 0
    output_tokens: int = 0
    raw: Any = None  # provider-native assistant content, replayed verbatim on the next turn
    model: str = ""
    detail: str = ""


@dataclass
class ModelConfig:
    id: str
    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""  # resolved secret (env var or decrypted), never persisted from here
    location: str = "cloud"
    enabled: bool = True
    cost_in: float = 0.0  # USD per million input tokens
    cost_out: float = 0.0
    options: dict = field(default_factory=dict)


class ProviderError(Exception):
    """A model call failed. ``kind`` is one of: auth, rate_limit, bad_request,
    unavailable, connection, refusal, other."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


class Provider(ABC):
    name = "base"

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg

    @property
    def supports_tools(self) -> bool:
        return bool(self.cfg.options.get("supports_tools", True))

    def available(self) -> tuple[bool, str]:
        """Cheap, offline check (credentials present, etc.)."""
        return True, "ok"

    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        purpose: str = "answer",
    ) -> ChatResult:
        """One model turn. ``purpose`` is 'answer', 'triage' or 'review'."""

    async def health(self) -> tuple[bool, str]:
        """Live check used by the console's Test button."""
        try:
            result = await self.chat("Reply with the single word: ok", [{"role": "user", "content": "ping"}],
                                    max_tokens=2000, purpose="triage")
            return True, f"responded ({result.model or self.cfg.model})"
        except ProviderError as exc:
            return False, str(exc)
