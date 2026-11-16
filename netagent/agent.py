"""One agent working one task on one model: prompt assembly and the tool loop."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable

from .models.base import Provider
from .tools import Tool, ToolContext, run_tool

Emit = Callable[[str, str, dict], None]

_CONFIDENCE = re.compile(r"^[ \t>*_`#-]*CONFIDENCE[*_`]*\s*[:=]\s*[*_`]*\s*(\d{1,3})\s*%?[*_`.]*\s*$", re.I | re.M)
_ESCALATE = re.compile(r"^\s*[*_`]*ESCALATE[*_`]*\s*:\s*(.+)", re.I)

MISSING_CONFIDENCE = 60  # a model that does not self-assess is treated as unsure


@dataclass
class Attempt:
    outcome: str  # answer | escalate | error
    text: str = ""
    confidence: int | None = None
    reason: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    steps: int = 0
    evidence: list[dict] = field(default_factory=list)


def build_system_prompt(agent: dict, profile: dict, tools: list[Tool], final_tier: bool, tools_enabled: bool) -> str:
    skills = set(profile.get("skills") or [])
    parts = [
        f"You are {agent['name']}, an AI agent on the NetAgent platform.",
        f"Profile: {profile['name']} - {profile.get('description', '')}".rstrip(" -"),
        f"Agent role: {'coordinator' if profile.get('coordinator') else 'specialist'}",
    ]
    if profile.get("personality"):
        parts += ["", "## Personality and tone", profile["personality"].strip()]
    if profile.get("system_prompt"):
        parts += ["", "## Your brief", profile["system_prompt"].strip()]

    work = [
        "Tasks reach you from people (web console, Microsoft Teams, email, API) and from other agents. Every task "
        "is tracked, and your final message is delivered to the requester as the task result - write it for them: "
        "lead with the answer or outcome, then the supporting detail, commands and caveats.",
        "Work from evidence. Never invent device output, configuration or facts. If you cannot observe something, "
        "say exactly what should be run and what to look for in the result.",
    ]
    if tools_enabled and tools:
        work.append("Use your tools when they give you facts you would otherwise have to guess.")
        if "device-access" in skills:
            work.append(
                "Device safety: you may run read-only commands on inventory devices yourself. You can never change "
                "a device directly. Every configuration change goes through propose_config_change and is applied "
                "only after a human approves it; always supply rollback and verification commands, and state the "
                "expected impact. Credentials in device output are redacted before you see them - never ask for "
                "passwords."
            )
        if "collaboration" in skills:
            work.append(
                "Collaboration: other agents have different specialities. When part of a task sits outside your "
                "expertise, consult the right agent (list_agents shows who is available) instead of guessing, and "
                "say in your answer which parts came from whom."
            )
    else:
        work.append(
            "You have no tools in this run. Answer from the information in the task; where live device data would "
            "be needed, list the commands to run."
        )
    parts += ["", "## How you work", *[f"- {w}" for w in work]]

    protocol = [
        "Write your final answer in Markdown.",
        "End your final answer with a line of the form `CONFIDENCE: <0-100>`: your honest estimate that the answer "
        "is correct and complete. It is used for routing, so do not inflate it.",
    ]
    if final_tier:
        protocol.append(
            "You are the most capable model available for this task; there is no further escalation. If something "
            "cannot be determined, say what is missing and give your best supported assessment."
        )
    else:
        protocol.append(
            "You are one tier of a ladder of models. If the task needs deeper expertise or more careful reasoning "
            "than you can give reliably, do not guess: reply with only `ESCALATE: <short reason>` and it is handed "
            "to a more capable model. Handle simple requests yourself."
        )
    parts += ["", "## Response protocol", *[f"- {p}" for p in protocol]]
    return "\n".join(parts)


def parse_final(text: str) -> tuple[str, str, int | None, str]:
    """Returns (outcome, body, confidence, reason)."""
    stripped = text.strip()
    if not stripped:
        return "escalate", "", None, "the model returned an empty response"
    escalate = _ESCALATE.match(stripped)
    if escalate:
        return "escalate", "", None, escalate.group(1).strip().splitlines()[0][:300]
    confidence = None
    matches = list(_CONFIDENCE.finditer(stripped))
    if matches:
        last = matches[-1]
        confidence = max(0, min(100, int(last.group(1))))
        stripped = (stripped[: last.start()] + stripped[last.end():]).strip()
    return "answer", stripped, confidence, ""


def _short_args(arguments: dict) -> str:
    text = json.dumps(arguments, ensure_ascii=False)
    return text if len(text) <= 200 else text[:200] + "..."


async def run_attempt(
    provider: Provider,
    system: str,
    messages: list[dict],
    tools: list[Tool],
    ctx: ToolContext,
    emit: Emit,
    max_steps: int = 12,
) -> Attempt:
    """Drive one model through the task until it produces a final answer."""
    attempt = Attempt(outcome="error")
    use_tools = provider.supports_tools and bool(tools)
    specs = [t.spec for t in tools] if use_tools else None
    allowed = {t.spec.name for t in tools}

    def finish(result_text: str) -> Attempt:
        outcome, body, confidence, reason = parse_final(result_text)
        attempt.outcome, attempt.text, attempt.confidence, attempt.reason = outcome, body, confidence, reason
        return attempt

    for step in range(max_steps):
        attempt.steps = step + 1
        result = await provider.chat(system, messages, specs)
        attempt.input_tokens += result.input_tokens
        attempt.output_tokens += result.output_tokens
        if result.stop_reason == "refusal":
            attempt.outcome, attempt.reason = "error", f"the model declined the request ({result.detail or 'refusal'})"
            return attempt
        messages.append({
            "role": "assistant", "content": result.text, "tool_calls": result.tool_calls,
            "raw": result.raw, "raw_provider": provider.name,
        })
        if result.tool_calls:
            for call in result.tool_calls:
                emit("tool", f"{call.name} {_short_args(call.arguments)}", {"tool": call.name, "arguments": call.arguments})
                output, is_error = await run_tool(ctx, call.name, call.arguments, allowed)
                messages.append({
                    "role": "tool", "tool_call_id": call.id, "name": call.name, "content": output, "is_error": is_error,
                })
                attempt.evidence.append({
                    "tool": call.name, "arguments": call.arguments, "output": output[:2500], "is_error": is_error,
                })
                if is_error:
                    emit("tool_error", f"{call.name}: {output[:300]}", {"tool": call.name})
            continue
        if result.stop_reason == "pause":
            continue
        if result.stop_reason == "max_tokens":
            done = finish(result.text)
            if done.outcome == "answer":
                done.text += "\n\n_[The response hit the output limit and may be incomplete.]_"
                done.confidence = min(done.confidence or 40, 40)
            return done
        return finish(result.text)

    # Step budget exhausted: ask for a conclusion with what has been gathered.
    messages.append({
        "role": "user",
        "content": "You have reached the step limit for this task. Do not call any more tools. Give your final "
                   "answer now from what you have gathered, noting anything left unverified.",
    })
    # The tool list stays on the request: history that contains tool calls must keep its tool definitions.
    result = await provider.chat(system, messages, specs)
    attempt.input_tokens += result.input_tokens
    attempt.output_tokens += result.output_tokens
    attempt.steps += 1
    if result.tool_calls and not result.text.strip():
        attempt.outcome, attempt.reason = "escalate", f"step limit of {max_steps} reached without a conclusion"
        return attempt
    return finish(result.text)
