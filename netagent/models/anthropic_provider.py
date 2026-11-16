"""Claude models through the official Anthropic SDK."""
from __future__ import annotations

import os
from pathlib import Path

import anthropic

from .base import ChatResult, ModelConfig, Provider, ProviderError, ToolCall, ToolSpec

# Models whose safety classifiers can decline a request and that support
# server-side fallback, so a declined turn is re-run instead of failing the task.
_FALLBACK_MODELS = ("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5", "claude-mythos-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause",
}


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, cfg: ModelConfig):
        super().__init__(cfg)
        self._client: anthropic.AsyncAnthropic | None = None

    # -- setup -----------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if self.cfg.api_key:
            return True, "ok"
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True, "ok"
        if (Path.home() / ".config" / "anthropic").is_dir():
            return True, "ok (ant profile)"
        return False, "no Anthropic credentials (set ANTHROPIC_API_KEY or add a key to the model)"

    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            kwargs: dict = {
                "timeout": float(self.cfg.options.get("timeout", 600)),
                "max_retries": int(self.cfg.options.get("max_retries", 2)),
            }
            if self.cfg.api_key:
                kwargs["api_key"] = self.cfg.api_key
            if self.cfg.base_url:
                kwargs["base_url"] = self.cfg.base_url
            self._client = anthropic.AsyncAnthropic(**kwargs)
        return self._client

    def _use_fallbacks(self) -> bool:
        setting = str(self.cfg.options.get("refusal_fallback", "auto")).lower()
        if setting in {"off", "false", "0"}:
            return False
        if setting in {"on", "true", "1"}:
            return True
        # auto: first-party API only; Bedrock/Vertex/Foundry gateways reject the parameter.
        return not self.cfg.base_url and self.cfg.model.startswith(_FALLBACK_MODELS)

    # -- conversion ------------------------------------------------------
    @staticmethod
    def _convert(messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        results: list[dict] = []

        def flush() -> None:
            nonlocal results
            if results:
                # All tool results for one assistant turn go back in a single user message.
                out.append({"role": "user", "content": results})
                results = []

        for msg in messages:
            role = msg["role"]
            if role == "tool":
                block = {"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": msg["content"]}
                if msg.get("is_error"):
                    block["is_error"] = True
                results.append(block)
                continue
            flush()
            if role == "user":
                out.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                if msg.get("raw_provider") == "anthropic" and msg.get("raw") is not None:
                    # Replay the original blocks untouched (thinking blocks included).
                    out.append({"role": "assistant", "content": msg["raw"]})
                    continue
                blocks: list[dict] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
        flush()
        return out

    # -- chat ------------------------------------------------------------
    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        purpose: str = "answer",
    ) -> ChatResult:
        opts = self.cfg.options
        kwargs: dict = {
            "model": self.cfg.model,
            "max_tokens": int(max_tokens or opts.get("max_tokens", 16000)),
            "system": system,
            "messages": self._convert(messages),
            # The system prompt and tool list are identical on every turn of a task;
            # cache them so each agent-loop iteration only pays for what is new.
            "cache_control": {"type": "ephemeral"},
        }
        if tools and self.supports_tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        # Effort is only sent for models configured with it (older models reject the field).
        if opts.get("effort"):
            effort = opts["effort"] if purpose == "answer" else opts.get("triage_effort", "low")
            kwargs["output_config"] = {"effort": effort}

        client = self.client()
        try:
            if self._use_fallbacks():
                response = await client.beta.messages.create(
                    betas=[_FALLBACK_BETA], fallbacks="default", **kwargs
                )
            else:
                response = await client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise ProviderError(f"Anthropic authentication failed: {exc.message}", "auth") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ProviderError(f"Anthropic permission denied: {exc.message}", "auth") from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"Model '{self.cfg.model}' not found: {exc.message}", "bad_request") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"Anthropic rate limit: {exc.message}", "rate_limit") from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"Anthropic rejected the request: {exc.message}", "bad_request") from exc
        except anthropic.APIStatusError as exc:
            kind = "unavailable" if exc.status_code >= 500 else "other"
            raise ProviderError(f"Anthropic API error {exc.status_code}: {exc.message}", kind) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Cannot reach the Anthropic API: {exc}", "connection") from exc

        result = ChatResult(
            stop_reason=_STOP.get(response.stop_reason or "end_turn", "end"),
            input_tokens=(response.usage.input_tokens or 0)
            + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
            + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0),
            output_tokens=response.usage.output_tokens or 0,
            raw=response.content,
            model=response.model,
        )
        if result.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            result.detail = getattr(details, "category", None) or "declined by safety classifiers"
            return result  # content is not meaningful on a refusal
        texts: list[str] = []
        for block in response.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                result.tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input or {})))
        result.text = "\n".join(t for t in texts if t).strip()
        return result

    async def health(self) -> tuple[bool, str]:
        ok, why = self.available()
        if not ok:
            return False, why
        try:
            info = await self.client().models.retrieve(self.cfg.model)
            return True, f"reachable ({info.display_name})"
        except anthropic.AuthenticationError as exc:
            return False, f"authentication failed: {exc.message}"
        except anthropic.NotFoundError:
            return False, f"model '{self.cfg.model}' not found"
        except anthropic.APIStatusError as exc:
            return False, f"API error {exc.status_code}: {exc.message}"
        except anthropic.APIConnectionError as exc:
            return False, f"cannot reach the API: {exc}"
