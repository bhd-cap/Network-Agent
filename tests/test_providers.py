"""Model adapters: request/response translation for Claude and OpenAI-compatible endpoints (no real API calls)."""
import json

import anthropic
import httpx
import httpx2
import pytest

from netagent.models.anthropic_provider import AnthropicProvider
from netagent.models.base import ModelConfig, ProviderError, ToolCall, ToolSpec
from netagent.models.openai_compat import OpenAICompatProvider

TOOLS = [ToolSpec("subnet_calc", "Calculate subnets", {"type": "object", "properties": {"cidr": {"type": "string"}}, "required": ["cidr"]})]


# ----------------------------------------------------------------- Claude
def claude(model="claude-opus-5-5", handler=None, **options):
    provider = AnthropicProvider(ModelConfig(id="c", name="Claude", provider="anthropic", model=model, api_key="test-key", options=options))
    provider._client = anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0, http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))
    return provider


def message(content, stop_reason="end_turn", **extra):
    return httpx2.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                      "content": content, "stop_reason": stop_reason, "stop_sequence": None,
                                      "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}, **extra})


async def test_claude_request_shape_and_tool_round_trip():
    seen = []

    def handler(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        if len(seen) == 1:
            return message([{"type": "thinking", "thinking": "", "signature": "sig"},
                            {"type": "text", "text": "Let me calculate."},
                            {"type": "tool_use", "id": "tu_1", "name": "subnet_calc", "input": {"cidr": "10.0.0.0/24"}}], "tool_use")
        return message([{"type": "text", "text": "254 hosts.\nCONFIDENCE: 97"}])

    provider = claude(handler=handler, effort="high")
    history = [{"role": "user", "content": "hosts in 10.0.0.0/24?"}]
    first = await provider.chat("You are a network engineer.", history, TOOLS)
    assert first.stop_reason == "tool_use" and first.text == "Let me calculate."
    assert first.tool_calls == [ToolCall(id="tu_1", name="subnet_calc", arguments={"cidr": "10.0.0.0/24"})]
    assert first.input_tokens == 150 and first.output_tokens == 20

    headers, body = seen[0]
    assert body["model"] == "claude-opus-5-5" and body["system"] == "You are a network engineer."
    assert body["tools"] == [{"name": "subnet_calc", "description": "Calculate subnets", "input_schema": TOOLS[0].parameters}]
    assert body["output_config"] == {"effort": "high"} and body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    for removed in ("temperature", "top_p", "top_k", "thinking", "tool_choice"):
        assert removed not in body  # rejected or unnecessary on current models

    history += [
        {"role": "assistant", "content": first.text, "tool_calls": first.tool_calls, "raw": first.raw, "raw_provider": "anthropic"},
        {"role": "tool", "tool_call_id": "tu_1", "name": "subnet_calc", "content": "Usable hosts: 254", "is_error": False},
    ]
    second = await provider.chat("You are a network engineer.", history, TOOLS)
    assert second.stop_reason == "end" and "254 hosts" in second.text
    sent = seen[1][1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    # the assistant turn is replayed unchanged, thinking block included
    assert [b["type"] for b in sent[1]["content"]] == ["thinking", "text", "tool_use"] and sent[1]["content"][0]["signature"] == "sig"
    assert sent[2]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "Usable hosts: 254"}]


async def test_claude_parallel_tool_results_share_one_user_message():
    calls = [ToolCall("a", "subnet_calc", {"cidr": "10.0.0.0/24"}), ToolCall("b", "subnet_calc", {"cidr": "10.0.1.0/24"})]
    converted = AnthropicProvider._convert([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": calls},  # from another provider: rebuilt from parts
        {"role": "tool", "tool_call_id": "a", "content": "one"},
        {"role": "tool", "tool_call_id": "b", "content": "boom", "is_error": True},
    ])
    assert [m["role"] for m in converted] == ["user", "assistant", "user"]
    assert [b["type"] for b in converted[1]["content"]] == ["tool_use", "tool_use"]
    assert converted[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "a", "content": "one"},
        {"type": "tool_result", "tool_use_id": "b", "content": "boom", "is_error": True},
    ]


async def test_claude_haiku_has_no_fallback_and_triage_uses_low_effort():
    seen = []

    def handler(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        return message([{"type": "text", "text": '{"complexity": 2}'}])

    provider = claude("claude-haiku-5-5", handler, effort="medium")
    await provider.chat("triage", [{"role": "user", "content": "x"}], max_tokens=2000, purpose="triage")
    headers, body = seen[0]
    assert "fallbacks" not in body and "anthropic-beta" not in headers
    assert body["output_config"] == {"effort": "low"} and body["max_tokens"] == 2000 and "tools" not in body

    plain = claude("claude-haiku-4-5", handler)  # older model configured without effort
    await plain.chat("s", [{"role": "user", "content": "x"}])
    assert "output_config" not in seen[1][1]


async def test_claude_refusal_and_errors():
    refusing = claude(handler=lambda r: message([], "refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": "x"}))
    result = await refusing.chat("s", [{"role": "user", "content": "x"}])
    assert result.stop_reason == "refusal" and result.detail == "cyber" and result.text == ""

    def error(status, kind):
        return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": "nope"}})

    for status, api_kind, expected in [(401, "authentication_error", "auth"), (429, "rate_limit_error", "rate_limit"),
                                       (400, "invalid_request_error", "bad_request"), (404, "not_found_error", "bad_request"),
                                       (529, "overloaded_error", "unavailable")]:
        with pytest.raises(ProviderError) as excinfo:
            await claude(handler=lambda r, s=status, k=api_kind: error(s, k)).chat("s", [{"role": "user", "content": "x"}])
        assert excinfo.value.kind == expected, status


def test_claude_availability(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    no_key = AnthropicProvider(ModelConfig(id="c", name="c", provider="anthropic", model="claude-haiku-5-5"))
    assert no_key.available()[0] is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert no_key.available()[0] is True


# ------------------------------------------------------ OpenAI-compatible
def compat(handler, provider="ollama", **cfg):
    p = OpenAICompatProvider(ModelConfig(id="l", name="Local", provider=provider, model="llama3.2:3b", **cfg))
    p._transport = httpx.MockTransport(handler)
    return p


async def test_openai_compat_round_trip():
    seen = []

    def handler(request):
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        if len(seen) == 1:
            return httpx.Response(200, json={"model": "llama3.2:3b", "choices": [{"finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "<think>hmm</think>",
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "subnet_calc", "arguments": "{\"cidr\": \"10.0.0.0/24\"}"}}]}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 9}})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "254.\nCONFIDENCE: 90"}}]})

    provider = compat(handler, base_url="http://ollama.internal:11434")
    history = [{"role": "user", "content": "hosts?"}]
    first = await provider.chat("system text", history, TOOLS, max_tokens=500)
    assert first.stop_reason == "tool_use" and first.text == "" and first.tool_calls[0].arguments == {"cidr": "10.0.0.0/24"}
    assert (first.input_tokens, first.output_tokens) == (40, 9)
    url, headers, body = seen[0]
    assert url == "http://ollama.internal:11434/v1/chat/completions" and "authorization" not in headers
    assert body["messages"][0] == {"role": "system", "content": "system text"} and body["max_tokens"] == 500
    assert body["tools"][0] == {"type": "function", "function": {"name": "subnet_calc", "description": "Calculate subnets", "parameters": TOOLS[0].parameters}}

    history += [{"role": "assistant", "content": "", "tool_calls": first.tool_calls},
                {"role": "tool", "tool_call_id": "call_1", "name": "subnet_calc", "content": "bad cidr", "is_error": True}]
    second = await provider.chat("system text", history, TOOLS)
    assert second.stop_reason == "end" and second.text.startswith("254.")
    sent = seen[1][2]["messages"]
    assert sent[2]["tool_calls"][0]["function"] == {"name": "subnet_calc", "arguments": "{\"cidr\": \"10.0.0.0/24\"}"}
    assert sent[3] == {"role": "tool", "tool_call_id": "call_1", "content": "ERROR: bad cidr"}


async def test_openai_compat_options_auth_and_errors():
    seen = []

    def ok(request):
        seen.append((dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "hi", "tool_calls": [
            {"function": {"name": "subnet_calc", "arguments": "{not json"}}]}}]})

    keyed = compat(ok, provider="openai_compatible", base_url="https://gw.example/v1", api_key="sk-1",
                   options={"supports_tools": False, "max_tokens_param": "max_completion_tokens", "max_tokens": 900})
    result = await keyed.chat("s", [{"role": "user", "content": "x"}], TOOLS)
    headers, body = seen[0]
    assert headers["authorization"] == "Bearer sk-1" and "tools" not in body and body["max_completion_tokens"] == 900
    assert result.tool_calls[0].arguments == {"_invalid_json": "{not json"}  # surfaced to the model as a tool error

    azure = compat(ok, provider="azure_openai", base_url="https://x.openai.azure.com/openai/v1", api_key="az")
    await azure.chat("s", [{"role": "user", "content": "x"}])
    assert seen[1][0]["api-key"] == "az" and "authorization" not in seen[1][0]

    def refuse(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ProviderError) as excinfo:
        await compat(refuse).chat("s", [{"role": "user", "content": "x"}])
    assert excinfo.value.kind == "connection"
    with pytest.raises(ProviderError) as excinfo:
        await compat(lambda r: httpx.Response(401, text="bad key")).chat("s", [{"role": "user", "content": "x"}])
    assert excinfo.value.kind == "auth"
    assert OpenAICompatProvider(ModelConfig(id="o", name="o", provider="openai", model="m")).available()[0] is False


async def test_ollama_health_reports_missing_model():
    up = compat(lambda r: httpx.Response(200, json={"data": [{"id": "qwen2.5:14b"}]}))
    ok, message_text = await up.health()
    assert not ok and "ollama pull llama3.2:3b" in message_text
    ok, _ = await compat(lambda r: httpx.Response(200, json={"data": [{"id": "llama3.2:3b"}]})).health()
    assert ok
