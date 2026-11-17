"""Offline stand-in model.

Used by the test-suite and by demo mode (``NETAGENT_DEMO=1``) so the whole
platform - ladder, tools, multi-agent hand-offs, approvals, channels - can be
exercised with no model runtime and no API key. It follows the same response
protocol real models are asked to follow.

Options:
    capability: 1-5, the highest task complexity this fake model is "confident" on.
    script:     list of canned turns ({"text": ..., "tool_calls": [{"name":..., "arguments":...}]}),
                consumed in order; used by tests for exact control.
    fail:       raise a ProviderError on every call (simulates an outage).
"""
from __future__ import annotations

import json
import re
import uuid

from .base import ChatResult, Provider, ProviderError, ToolCall, ToolSpec

_CIDR = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
_DEVICE = re.compile(r"\b(?:on|device|from|at)\s+([A-Za-z][\w-]*-[\w-]*\d\w*)", re.I)
_VLAN = re.compile(r"\bvlan\s+(\d{1,4})(?:\s+(?:named|name)\s+([\w-]+))?", re.I)
_VENDORS = {"fortinet": "fortinet", "fortigate": "fortinet", "fortios": "fortinet", "arista": "arista",
            "eos": "arista", "cisco": "cisco", "nexus": "cisco", "catalyst": "cisco"}

_HARD = ("design", "architecture", "migrate", "migration", "evpn", "vxlan", "root cause", "intermittent",
         "redesign", "multi-vendor", "sd-wan")
_MEDIUM = ("troubleshoot", "bgp", "ospf", "vpn", "ipsec", "not working", "down", "flapping", "configure",
           "config", "failover", "mlag", "spanning", "packet loss", "firewall policy")
_CHANGE = ("configure", "add ", "apply", "push", "change ", "remove ", "create ")


def estimate_complexity(text: str) -> int:
    low = text.lower()
    if any(k in low for k in _HARD):
        return 5 if len(low) > 400 else 4
    if any(k in low for k in _MEDIUM):
        return 3
    return 1 if len(low) < 160 else 2


def _task_text(messages: list[dict]) -> str:
    for msg in messages:
        if msg["role"] == "user":
            return str(msg["content"])
    return ""


class MockProvider(Provider):
    name = "mock"

    def __init__(self, cfg):
        super().__init__(cfg)
        self._script = list(cfg.options.get("script") or [])

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolSpec] | None = None,
        max_tokens: int | None = None,
        purpose: str = "answer",
    ) -> ChatResult:
        if self.cfg.options.get("fail"):
            raise ProviderError(f"{self.cfg.name}: simulated outage", "connection")
        text = _task_text(messages)
        approx_in = sum(len(str(m.get("content", ""))) for m in messages) // 4 + len(system) // 4

        def done(body: str, calls: list[ToolCall] | None = None) -> ChatResult:
            return ChatResult(
                text=body,
                tool_calls=calls or [],
                stop_reason="tool_use" if calls else "end",
                input_tokens=approx_in,
                output_tokens=max(1, len(body) // 4),
                model=self.cfg.model,
            )

        if purpose == "triage":
            low = text.lower()
            category = (
                "config_change" if any(k in low for k in _CHANGE)
                else "troubleshooting" if any(k in low for k in ("troubleshoot", "down", "fail", "issue", "problem", "flapping"))
                else "question"
            )
            risk = "high" if category == "config_change" else "medium" if category == "troubleshooting" else "low"
            return done(json.dumps({
                "complexity": estimate_complexity(text), "category": category, "risk": risk,
                "summary": text.strip().splitlines()[0][:120] if text.strip() else "",
            }))
        if purpose == "review":
            return done("VERDICT: APPROVE")

        if self._script:
            turn = self._script.pop(0)
            calls = [
                ToolCall(id=f"mock_{uuid.uuid4().hex[:10]}", name=c["name"], arguments=c.get("arguments", {}))
                for c in turn.get("tool_calls", [])
            ]
            return done(turn.get("text", ""), calls)

        offered = {t.name for t in tools or []} if self.supports_tools else set()
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        used = {m.get("name") for m in tool_msgs}
        low = text.lower()

        def call(name: str, **arguments) -> ChatResult:
            return done("", [ToolCall(id=f"mock_{uuid.uuid4().hex[:10]}", name=name, arguments=arguments)])

        # --- a small scripted policy so demos exercise the real tool paths -----
        vendor = next((v for k, v in _VENDORS.items() if k in low), None)
        if "Agent role: coordinator" in system and vendor and "consult_agent" in offered:
            if "list_agents" not in used:
                return call("list_agents")
            if "consult_agent" not in used:
                roster = next(m["content"] for m in tool_msgs if m.get("name") == "list_agents")
                for line in roster.splitlines():
                    match = re.match(r"-\s+\*\*(.+?)\*\*", line)
                    if match and vendor in line.lower() and "coordinator" not in line.lower():
                        question = text.split("\n\n", 1)[-1].strip()[:1500]
                        return call("consult_agent", agent=match.group(1), question=question)
        cidr = _CIDR.search(text)
        if cidr and "subnet_calc" in offered and "subnet_calc" not in used:
            return call("subnet_calc", cidr=cidr.group(1))
        device = _DEVICE.search(text)
        vlan = _VLAN.search(text)
        if device and vlan and any(k in low for k in _CHANGE) and "propose_config_change" in offered \
                and "propose_config_change" not in used:
            vid, vname = vlan.group(1), vlan.group(2) or f"VLAN{vlan.group(1)}"
            return call(
                "propose_config_change", device=device.group(1), summary=f"Create VLAN {vid} ({vname})",
                rationale="Requested in the task.", risk="low",
                commands=[f"vlan {vid}", f" name {vname}"], rollback_commands=[f"no vlan {vid}"],
                verify_commands=[f"show vlan id {vid}"],
            )
        if device and "run_show_command" in offered and "run_show_command" not in used and not vlan:
            command = (
                "show ip bgp summary" if "bgp" in low
                else "show ip interface brief" if "interface" in low
                else "show version"
            )
            return call("run_show_command", device=device.group(1), command=command)

        # --- final answer ------------------------------------------------------
        complexity = estimate_complexity(text)
        capability = int(self.cfg.options.get("capability", 3))
        confidence = 92 if complexity <= capability else 45
        lines = [f"**[{self.cfg.name}]** Demo answer from the offline mock model.", ""]
        if tool_msgs:
            lines.append("Evidence gathered:")
            for m in tool_msgs:
                snippet = str(m["content"]).strip()
                lines.append(f"\n*{m.get('name')}*\n```\n{snippet[:900]}\n```")
        else:
            lines.append(f"Request understood: {text.strip().splitlines()[0][:200] if text.strip() else '(empty)'}")
        lines.append("")
        lines.append(
            "Connect a real model (Ollama, any OpenAI-compatible endpoint, or Claude) under "
            "**Models & Ladders** to get real analysis."
        )
        lines.append(f"\nCONFIDENCE: {confidence}")
        return done("\n".join(lines))

    async def health(self) -> tuple[bool, str]:
        if self.cfg.options.get("fail"):
            return False, "simulated outage"
        return True, "mock model (offline demo)"
