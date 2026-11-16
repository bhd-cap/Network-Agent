"""The escalation ladder.

A ladder is an ordered list of model tiers, cheapest/fastest first (typically a
small local model, then progressively larger cloud models).

1. **Triage** - the smallest available model classifies the task (complexity,
   category, risk). Trivial work starts at the bottom; hard or risky work skips
   straight to a tier that can handle it.
2. **Attempt** - the chosen tier works the task. It either answers with a
   self-assessed confidence, or asks to escalate.
3. **Escalate** - low confidence, an explicit ESCALATE, or a model outage hands
   the task (plus the evidence already gathered) to the next tier up.
4. **Review** (optional) - for risky work, the next tier up checks a confident
   answer before it is released.

Tiers whose model is unavailable (no key, runtime down) are skipped, so the same
ladder degrades gracefully to local-only or cloud-only operation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from .agent import MISSING_CONFIDENCE, Attempt, Emit, run_attempt
from .models.base import Provider, ProviderError
from .tools import Tool, ToolContext

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

TRIAGE_SYSTEM = """You are the dispatcher for an AI operations team. Classify the incoming task so it can be \
routed to a model of the right size. Respond with ONLY a JSON object, no prose:

{"complexity": <1-5>, "category": "question|troubleshooting|config_change|design|other", "risk": "low|medium|high", \
"summary": "<one line>"}

Complexity: 1 = factual lookup or definition. 2 = simple how-to, a single command or short config snippet. \
3 = standard troubleshooting or a single-device configuration task. 4 = multi-step or multi-device problem, or \
analysis of substantial configuration/output. 5 = architecture and design, migrations, ambiguous multi-vendor incidents.
Risk is the blast radius if the answer is wrong or a change is applied: high for changes to production devices, \
low for explanations."""

REVIEW_SYSTEM = """You are a senior reviewer. A junior colleague answered the task below. Check the answer for \
technical errors, unsafe steps and anything important that is missing. Reply with exactly one line:

VERDICT: APPROVE
or
VERDICT: REJECT - <the specific problem>

Approve answers that are correct and adequate even if you would have phrased them differently."""

_DOWN_KINDS = {"connection", "auth", "unavailable", "rate_limit"}


class LadderError(Exception):
    pass


@dataclass
class LadderResult:
    text: str
    confidence: int | None
    needs_review: bool
    model_id: str
    model_name: str
    escalations: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    triage: dict = field(default_factory=dict)
    trail: list[dict] = field(default_factory=list)


def parse_triage(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        complexity = max(1, min(5, int(data.get("complexity"))))
    except (TypeError, ValueError):
        return {}
    category = str(data.get("category") or "other").lower()
    if category not in {"question", "troubleshooting", "config_change", "design", "other"}:
        category = "other"
    risk = str(data.get("risk") or "medium").lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    return {"complexity": complexity, "category": category, "risk": risk, "summary": str(data.get("summary") or "")[:200]}


def _cost(provider: Provider, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * provider.cfg.cost_in + tokens_out * provider.cfg.cost_out) / 1_000_000


def _handoff(attempt: Attempt | None, model_name: str, reason: str) -> str:
    lines = [
        "", "---", "## Handoff from a lower tier",
        f"A smaller model ({model_name}) worked on this task first and it was escalated to you. Reason: {reason}.",
        "Treat its work as unverified input, not as fact.",
    ]
    if attempt and attempt.text:
        lines += ["", "### Its draft answer", attempt.text[:5000]]
    if attempt and attempt.evidence:
        lines += ["", "### Tool results it gathered (reuse these rather than repeating the same calls)"]
        for item in attempt.evidence[-8:]:
            status = " [error]" if item["is_error"] else ""
            lines.append(f"\n`{item['tool']}` {json.dumps(item['arguments'], ensure_ascii=False)[:300]}{status}\n```\n{item['output']}\n```")
    return "\n".join(lines)


async def run_ladder(
    rt: "Runtime",
    ladder: dict,
    build_system: Callable[[bool, bool], str],
    task_message: str,
    tools: list[Tool],
    ctx: ToolContext,
    emit: Emit,
    max_steps: int = 12,
) -> LadderResult:
    """Run a task through the ladder. ``build_system(final_tier, tools_enabled)`` renders the system prompt."""
    options = ladder.get("options") or {}
    tiers = ladder.get("tiers") or []
    usable: list[tuple[int, dict, Provider]] = []
    skipped: list[str] = []
    for index, tier in enumerate(tiers):
        provider, why = rt.registry.usable(tier.get("model", ""))
        if provider:
            usable.append((index, tier, provider))
        else:
            skipped.append(f"tier {index + 1} ({tier.get('model')}): {why}")
    if skipped:
        emit("ladder", "Skipped unavailable tiers - " + "; ".join(skipped), {"skipped": skipped})
    if not usable:
        raise LadderError(
            f"No model in ladder '{ladder.get('name', ladder.get('id'))}' is available. " + "; ".join(skipped)
        )

    result = LadderResult(text="", confidence=None, needs_review=False, model_id="", model_name="", escalations=0)

    def spend(provider: Provider, tokens_in: int, tokens_out: int) -> None:
        result.input_tokens += tokens_in
        result.output_tokens += tokens_out
        result.cost += _cost(provider, tokens_in, tokens_out)

    def fail(index: int, tier: dict, exc: ProviderError) -> None:
        if exc.kind in _DOWN_KINDS:
            rt.registry.mark_down(tier["model"], str(exc))
        result.trail.append({"tier": index + 1, "model": tier["model"], "outcome": "error", "reason": str(exc)})

    # ---- 1. triage ---------------------------------------------------------
    position = 0
    if options.get("triage", True) and len(usable) > 1:
        for index, tier, provider in list(usable):
            try:
                reply = await provider.chat(
                    TRIAGE_SYSTEM, [{"role": "user", "content": task_message[:8000]}], max_tokens=2000, purpose="triage"
                )
            except ProviderError as exc:
                fail(index, tier, exc)
                usable = [u for u in usable if u[0] != index]
                emit("ladder", f"Tier {index + 1} ({provider.cfg.name}) unavailable for triage: {exc}", {})
                continue
            spend(provider, reply.input_tokens, reply.output_tokens)
            result.triage = parse_triage(reply.text)
            if result.triage:
                result.triage["by"] = provider.cfg.name
            break
        if not usable:
            raise LadderError("Every model tier failed during triage. " + "; ".join(t["reason"] for t in result.trail))
        if result.triage:
            complexity = result.triage["complexity"]
            position = next(
                (i for i, (_, tier, _) in enumerate(usable) if int(tier.get("max_complexity", 5)) >= complexity),
                len(usable) - 1,
            )
            risky = result.triage["risk"] == "high" or result.triage["category"] == "config_change"
            if risky:
                floor = int(options.get("high_risk_min_tier", 2)) - 1
                floor_pos = next((i for i, (index, _, _) in enumerate(usable) if index >= floor), len(usable) - 1)
                position = max(position, floor_pos)
            emit(
                "triage",
                f"Triage by {result.triage['by']}: complexity {complexity}/5, {result.triage['category']}, "
                f"risk {result.triage['risk']} -> starting at tier {usable[position][0] + 1} ({usable[position][2].cfg.name})",
                result.triage,
            )
        else:
            emit("triage", "Triage was inconclusive - starting at the first tier", {})
    risky = bool(result.triage) and (result.triage["risk"] == "high" or result.triage["category"] == "config_change")
    review_mode = str(options.get("review", "high_risk"))
    want_review = review_mode == "always" or (review_mode == "high_risk" and risky)

    # ---- 2-4. attempt / escalate / review -------------------------------------
    handoff = ""
    best: tuple[Attempt, int, dict, Provider] | None = None
    last_reason = ""
    first = position
    while position < len(usable):
        index, tier, provider = usable[position]
        final = position == len(usable) - 1
        emit("attempt", f"Tier {index + 1} - {provider.cfg.name} is working on the task",
             {"tier": index + 1, "model": tier["model"], "location": provider.cfg.location})
        messages = [{"role": "user", "content": task_message + handoff}]
        try:
            attempt = await run_attempt(
                provider, build_system(final, provider.supports_tools), messages, tools, ctx, emit, max_steps
            )
        except ProviderError as exc:
            fail(index, tier, exc)
            last_reason = str(exc)
            emit("escalation", f"Tier {index + 1} ({provider.cfg.name}) failed: {exc}", {"tier": index + 1, "error": str(exc)})
            position += 1
            continue
        spend(provider, attempt.input_tokens, attempt.output_tokens)

        reason = attempt.reason
        if attempt.outcome == "answer":
            confidence = attempt.confidence if attempt.confidence is not None else MISSING_CONFIDENCE
            threshold = int(tier.get("min_confidence", 70))
            if best is None or confidence >= (best[0].confidence or 0):
                best = (attempt, index, tier, provider)
            if final or confidence >= threshold:
                rejected = ""
                if not final and want_review:
                    rejected = await _review(usable[position + 1], task_message, attempt.text, spend, emit)
                if not rejected:
                    result.trail.append({"tier": index + 1, "model": tier["model"], "outcome": "answer",
                                         "confidence": attempt.confidence, "steps": attempt.steps})
                    result.text, result.confidence = attempt.text, attempt.confidence
                    result.needs_review = final and confidence < threshold
                    result.model_id, result.model_name = tier["model"], provider.cfg.name
                    result.escalations = position - first
                    return result
                reason = f"rejected in review: {rejected}"
            else:
                stated = "no self-assessment" if attempt.confidence is None else f"confidence {confidence}"
                reason = f"{stated}, below this tier's threshold of {threshold}"
        result.trail.append({"tier": index + 1, "model": tier["model"], "outcome": attempt.outcome, "reason": reason,
                             "confidence": attempt.confidence, "steps": attempt.steps})
        last_reason = reason
        if not final:
            emit("escalation", f"Escalating from tier {index + 1} ({provider.cfg.name}): {reason}",
                 {"tier": index + 1, "reason": reason})
            handoff = _handoff(attempt, provider.cfg.name, reason)
        position += 1

    # Nothing above produced an acceptable answer. Fall back to the best draft, flagged for a human.
    if best is not None:
        attempt, index, tier, provider = best
        emit("ladder", f"No higher tier could complete the task ({last_reason}). Returning the draft from "
                       f"{provider.cfg.name}, flagged for human review.", {})
        result.text, result.confidence, result.needs_review = attempt.text, attempt.confidence, True
        result.model_id, result.model_name = tier["model"], provider.cfg.name
        result.escalations = max(0, len(result.trail) - 1)
        return result
    raise LadderError(f"No model tier could complete the task. Last problem: {last_reason or 'unknown'}. "
                      "Check the models under Models & Ladders in the console.")


async def _review(next_tier: tuple[int, dict, Provider], task_message: str, answer: str, spend, emit: Emit) -> str:
    """Ask the next tier up to check an answer. Returns '' when approved, else the objection."""
    index, _, reviewer = next_tier
    prompt = f"## Task\n{task_message[:12000]}\n\n## Answer to review\n{answer[:12000]}"
    try:
        reply = await reviewer.chat(REVIEW_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=3000, purpose="review")
    except ProviderError as exc:
        emit("review", f"Review by {reviewer.cfg.name} could not run ({exc}); releasing the answer unreviewed", {})
        return ""
    spend(reviewer, reply.input_tokens, reply.output_tokens)
    match = re.search(r"VERDICT\s*:\s*(APPROVE|REJECT)\s*[-:]*\s*(.*)", reply.text, re.I | re.S)
    if match and match.group(1).upper() == "REJECT":
        objection = match.group(2).strip().splitlines()[0][:400] if match.group(2).strip() else "no reason given"
        emit("review", f"Review by {reviewer.cfg.name} (tier {index + 1}): rejected - {objection}", {"approved": False})
        return objection
    emit("review", f"Review by {reviewer.cfg.name} (tier {index + 1}): approved", {"approved": True})
    return ""
