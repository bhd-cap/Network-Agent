"""Tool registry.

Tools are grouped into *skills*. A profile lists the skills its agents have, and
an agent is offered exactly the tools of those skills.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..models.base import ToolSpec

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

MAX_TOOL_OUTPUT = 24_000

SKILLS: dict[str, dict] = {
    "network-core": {
        "name": "Network engineering toolkit",
        "description": "Subnet maths, config linting, config diffs, cross-vendor command reference and the "
                       "troubleshooting playbook library.",
    },
    "device-access": {
        "name": "Device access",
        "description": "Read-only show/get commands against inventory devices, and configuration change "
                       "requests that a human must approve before anything is pushed.",
    },
    "collaboration": {
        "name": "Agent collaboration",
        "description": "See the other agents, consult a specialist and delegate sub-tasks.",
    },
    "task-tracking": {
        "name": "Task tracking",
        "description": "Post progress notes on the task timeline.",
    },
}


@dataclass
class ToolContext:
    rt: "Runtime"
    agent: dict
    profile: dict
    task: dict


class ToolError(Exception):
    """Raised by a tool to report a failure back to the model."""


Handler = Callable[[ToolContext, dict], Awaitable[str]]


@dataclass
class Tool:
    spec: ToolSpec
    skill: str
    handler: Handler


REGISTRY: dict[str, Tool] = {}


def tool(name: str, skill: str, description: str, properties: dict | None = None,
         required: list[str] | None = None) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(
            spec=ToolSpec(
                name=name,
                description=description,
                parameters={"type": "object", "properties": properties or {}, "required": required or []},
            ),
            skill=skill,
            handler=fn,
        )
        return fn

    return register


def tools_for(skills: list[str]) -> list[Tool]:
    _load()
    wanted = set(skills) | {"task-tracking"}
    return [t for t in REGISTRY.values() if t.skill in wanted]


async def run_tool(ctx: ToolContext, name: str, arguments: dict[str, Any], allowed: set[str]) -> tuple[str, bool]:
    """Execute a tool call. Returns (output, is_error)."""
    _load()
    entry = REGISTRY.get(name)
    if entry is None or name not in allowed:
        return f"Unknown tool '{name}'. Available tools: {', '.join(sorted(allowed))}.", True
    if "_invalid_json" in arguments:
        return "The tool arguments were not valid JSON. Call the tool again with a valid JSON object.", True
    missing = [p for p in entry.spec.parameters.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}.", True
    try:
        output = await entry.handler(ctx, arguments)
    except ToolError as exc:
        return str(exc), True
    except Exception as exc:  # a tool bug must not kill the task; let the model see it
        return f"Tool '{name}' failed: {exc.__class__.__name__}: {exc}", True
    output = str(output)
    if len(output) > MAX_TOOL_OUTPUT:
        output = output[:MAX_TOOL_OUTPUT] + f"\n... [output truncated at {MAX_TOOL_OUTPUT} characters]"
    return output, False


_loaded = False


def _load() -> None:
    global _loaded
    if not _loaded:
        from . import collab, devices, network, tracking  # noqa: F401  (registers tools)

        _loaded = True
