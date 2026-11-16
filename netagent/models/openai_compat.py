"""Any endpoint that speaks the OpenAI chat-completions wire format.

This is what local runtimes expose (Ollama, LM Studio, llama.cpp server, vLLM,
LocalAI) and what most hosted gateways accept, so one adapter covers them all.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx

from .base import ChatResult, ModelConfig, Provider, ProviderError, ToolCall, ToolSpec

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)

DEFAULT_BASE_URLS = {
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
}


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, cfg: ModelConfig):
        super().__init__(cfg)
        base = (cfg.base_url or DEFAULT_BASE_URLS.get(cfg.provider, "")).rstrip("/")
        if cfg.provider == "ollama" and base and not base.endswith("/v1"):
            base += "/v1"
        self.base_url = base
        self._transport: httpx.AsyncBaseTransport | None = None  # injectable for tests

    def available(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "no base URL configured"
        if self.cfg.provider in {"openai", "azure_openai"} and not self.cfg.api_key:
            return False, "no API key configured"
        return True, "ok"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            if self.cfg.provider == "azure_openai":
                headers["api-key"] = self.cfg.api_key
            else:
                headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        return headers

    def _timeout(self) -> httpx.Timeout:
        # Local models on CPU can take minutes to answer but should connect instantly.
        return httpx.Timeout(float(self.cfg.options.get("timeout", 300)), connect=5.0)

    @staticmethod
    def _convert(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for msg in messages:
            role = msg["role"]
            if role == "user":
                out.append({"role": "user", "content": msg["content"]})
            elif role == "assistant":
                item: dict = {"role": "assistant", "content": msg.get("content") or ""}
                calls = msg.get("tool_calls") or []
                if calls:
                    item["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in calls
                    ]
                out.append(item)
            elif role == "tool":
                content = msg["content"]
                if msg.get("is_error"):
                    content = f"ERROR: {content}"
                out.append({"role": "tool", "tool_call_id": msg["tool_call_id"], "content": content})
        return out

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        purpose: str = "answer",
    ) -> ChatResult:
        opts = self.cfg.options
        body: dict = {"model": self.cfg.model, "messages": self._convert(system, messages), "stream": False}
        if tools and self.supports_tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        limit = max_tokens or opts.get("max_tokens")
        token_param = opts.get("max_tokens_param", "max_tokens")
        if limit and token_param:
            body[token_param] = int(limit)
        if "temperature" in opts:
            body["temperature"] = opts["temperature"]

        try:
            async with httpx.AsyncClient(timeout=self._timeout(), transport=self._transport) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.cfg.name}: timed out talking to {self.base_url}", "connection") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.cfg.name}: cannot reach {self.base_url} ({exc})", "connection") from exc

        if resp.status_code >= 400:
            detail = resp.text[:400]
            kind = {401: "auth", 403: "auth", 404: "bad_request", 429: "rate_limit"}.get(
                resp.status_code, "unavailable" if resp.status_code >= 500 else "bad_request"
            )
            raise ProviderError(f"{self.cfg.name}: HTTP {resp.status_code} - {detail}", kind)
        try:
            data = resp.json()
            choice = data["choices"][0]
            message = choice.get("message") or {}
        except (ValueError, KeyError, IndexError) as exc:
            raise ProviderError(f"{self.cfg.name}: unexpected response shape", "other") from exc

        text = message.get("content") or ""
        if isinstance(text, list):  # some gateways return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
        text = _THINK.sub("", text).strip()

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except ValueError:
                    args = {"_invalid_json": args}
            if not isinstance(args, dict):
                args = {}
            calls.append(ToolCall(id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}", name=fn.get("name", ""), arguments=args))

        finish = choice.get("finish_reason") or "stop"
        usage = data.get("usage") or {}
        return ChatResult(
            text=text,
            tool_calls=calls,
            stop_reason="tool_use" if calls else {"length": "max_tokens", "content_filter": "refusal"}.get(finish, "end"),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            model=data.get("model") or self.cfg.model,
        )

    async def health(self) -> tuple[bool, str]:
        ok, why = self.available()
        if not ok:
            return False, why
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), transport=self._transport) as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            return False, f"cannot reach {self.base_url} ({exc.__class__.__name__})"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code} from {self.base_url}/models"
        try:
            ids = [m.get("id", "") for m in resp.json().get("data", [])]
        except ValueError:
            return True, "reachable"
        if ids and self.cfg.model not in ids:
            hint = " - run `ollama pull " + self.cfg.model + "`" if self.cfg.provider == "ollama" else ""
            return False, f"endpoint is up but model '{self.cfg.model}' is not installed{hint}"
        return True, "reachable, model present"
