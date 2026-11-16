"""The agent loop and the escalation ladder, driven by scripted mock models."""
import pytest

from netagent.agent import build_system_prompt, parse_final, run_attempt
from netagent.ladder import LadderError, parse_triage, run_ladder
from netagent.tools import ToolContext, tools_for

from conftest import add_ladder, add_mock_model


def _ctx(rt):
    agent = rt.db.one("SELECT * FROM agents WHERE name = 'Cisco Expert'")
    profile = rt.db.get("profiles", agent["profile_id"])
    task = rt.orchestrator.create_task("test", "test", agent_id=agent["id"], inline=True)
    return ToolContext(rt=rt, agent=agent, profile=profile, task=task), tools_for(profile["skills"])


async def _run(rt, ladder, message="What is OSPF?"):
    ctx, tools = _ctx(rt)
    events = []
    result = await run_ladder(
        rt, ladder, lambda final, enabled: build_system_prompt(ctx.agent, ctx.profile, tools, final, enabled),
        message, tools, ctx, lambda kind, summary, data=None: events.append((kind, summary)),
    )
    return result, events


# ---------------------------------------------------------------- parsing
def test_parse_final_extracts_confidence_and_strips_it():
    outcome, body, confidence, _ = parse_final("The mask is 255.255.254.0.\n\nCONFIDENCE: 93")
    assert (outcome, body, confidence) == ("answer", "The mask is 255.255.254.0.", 93)


@pytest.mark.parametrize("line", ["**CONFIDENCE:** 80%", "Confidence: 80", "confidence = 80.", "> CONFIDENCE: 80"])
def test_parse_final_tolerates_formatting(line):
    assert parse_final(f"answer\n{line}")[2] == 80


def test_parse_final_without_confidence():
    assert parse_final("just an answer") == ("answer", "just an answer", None, "")


def test_parse_final_escalate_and_empty():
    assert parse_final("ESCALATE: needs design expertise")[:1] == ("escalate",)
    assert parse_final("ESCALATE: needs design expertise")[3] == "needs design expertise"
    assert parse_final("   ")[0] == "escalate"
    # ESCALATE mentioned mid-answer is not an escalation request
    assert parse_final("You should escalate: call the vendor.\nCONFIDENCE: 90")[0] == "answer"


def test_parse_triage():
    assert parse_triage('Sure! {"complexity": 4, "category": "design", "risk": "high", "summary": "x"} done')["complexity"] == 4
    assert parse_triage('{"complexity": 9, "category": "weird", "risk": "?"}') == {
        "complexity": 5, "category": "other", "risk": "medium", "summary": ""}
    assert parse_triage("no json here") == {}
    assert parse_triage('{"complexity": "high"}') == {}


# ------------------------------------------------------------- agent loop
async def test_tool_loop_runs_tools_and_returns_answer(rt):
    add_mock_model(rt, "scripted", script=[
        {"tool_calls": [{"name": "subnet_calc", "arguments": {"cidr": "192.168.10.0/26"}}]},
        {"text": "There are 62 usable hosts.\nCONFIDENCE: 95"},
    ])
    ctx, tools = _ctx(rt)
    events = []
    messages = [{"role": "user", "content": "hosts in 192.168.10.0/26?"}]
    attempt = await run_attempt(rt.registry.get("scripted"), "sys", messages, tools, ctx,
                                lambda k, s, d=None: events.append(k))
    assert attempt.outcome == "answer" and attempt.confidence == 95
    assert "62" in attempt.text
    assert attempt.evidence[0]["tool"] == "subnet_calc" and "Usable hosts: 62" in attempt.evidence[0]["output"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert "tool" in events


async def test_unknown_tool_and_bad_arguments_are_reported_to_the_model(rt):
    add_mock_model(rt, "scripted", script=[
        {"tool_calls": [{"name": "format_disk", "arguments": {}}, {"name": "subnet_calc", "arguments": {}}]},
        {"text": "done\nCONFIDENCE: 90"},
    ])
    ctx, tools = _ctx(rt)
    messages = [{"role": "user", "content": "x"}]
    attempt = await run_attempt(rt.registry.get("scripted"), "sys", messages, tools, ctx, lambda *a: None)
    errors = [m for m in messages if m["role"] == "tool"]
    assert errors[0]["is_error"] and "Unknown tool" in errors[0]["content"]
    assert errors[1]["is_error"] and "Missing required" in errors[1]["content"]
    assert attempt.outcome == "answer"


async def test_step_limit_asks_for_a_conclusion(rt):
    loop = {"tool_calls": [{"name": "subnet_calc", "arguments": {"cidr": "10.0.0.0/8"}}]}
    add_mock_model(rt, "scripted", script=[loop, loop, loop, {"text": "best effort\nCONFIDENCE: 55"}])
    ctx, tools = _ctx(rt)
    messages = [{"role": "user", "content": "x"}]
    attempt = await run_attempt(rt.registry.get("scripted"), "sys", messages, tools, ctx, lambda *a: None, max_steps=3)
    assert attempt.outcome == "answer" and attempt.text == "best effort"
    assert "step limit" in [m for m in messages if m["role"] == "user"][-1]["content"]


# ----------------------------------------------------------------- ladder
async def test_simple_task_stays_on_the_smallest_tier(rt):
    result, events = await _run(rt, rt.db.get("ladders", "demo"), "What does ARP do?")
    assert result.model_id == "demo-small" and result.escalations == 0 and not result.needs_review
    assert result.triage["complexity"] == 1
    assert any(kind == "triage" for kind, _ in events)


async def test_triage_starts_hard_tasks_higher(rt):
    hard = "Design an EVPN VXLAN migration. " + "Details. " * 80
    result, _ = await _run(rt, rt.db.get("ladders", "demo"), hard)
    assert result.triage["complexity"] == 5
    assert result.model_id == "demo-large" and result.escalations == 0


async def test_low_confidence_escalates_with_handoff(rt):
    add_mock_model(rt, "small", script=[{"text": "I think it is X.\nCONFIDENCE: 40"}])
    add_mock_model(rt, "big", script=[{"text": "It is definitely Y.\nCONFIDENCE: 96"}])
    ladder = add_ladder(rt, "t", [{"model": "small", "max_complexity": 5, "min_confidence": 75},
                                   {"model": "big", "max_complexity": 5, "min_confidence": 0}], triage=False)
    result, events = await _run(rt, ladder)
    assert result.text == "It is definitely Y." and result.model_id == "big" and result.escalations == 1
    assert any(kind == "escalation" and "confidence 40" in summary for kind, summary in events)
    assert [t["outcome"] for t in result.trail] == ["answer", "answer"]


async def test_explicit_escalate_and_missing_self_assessment(rt):
    add_mock_model(rt, "t1", script=[{"text": "ESCALATE: beyond me"}])
    add_mock_model(rt, "t2", script=[{"text": "an answer with no confidence line"}])
    add_mock_model(rt, "t3", script=[{"text": "final\nCONFIDENCE: 88"}])
    ladder = add_ladder(rt, "t", [{"model": "t1", "min_confidence": 70}, {"model": "t2", "min_confidence": 70},
                                   {"model": "t3", "min_confidence": 0}], triage=False)
    result, events = await _run(rt, ladder)
    assert result.model_id == "t3" and result.escalations == 2
    reasons = [s for k, s in events if k == "escalation"]
    assert "beyond me" in reasons[0] and "no self-assessment" in reasons[1]


async def test_unavailable_and_failing_tiers_are_skipped(rt):
    add_mock_model(rt, "down", fail=True)
    add_mock_model(rt, "ok", script=[{"text": "fine\nCONFIDENCE: 90"}])
    rt.db.insert("models", {"id": "nokey", "name": "nokey", "provider": "anthropic", "model": "claude-haiku-5-5"})
    ladder = add_ladder(rt, "t", [{"model": "nokey"}, {"model": "down", "min_confidence": 70},
                                   {"model": "ok", "min_confidence": 0}], triage=False)
    result, events = await _run(rt, ladder)
    assert result.model_id == "ok"
    assert any(k == "ladder" and "nokey" in s for k, s in events)
    assert rt.registry.down_reason("down")  # circuit breaker remembers the outage
    assert rt.registry.usable("down")[0] is None


async def test_top_tier_low_confidence_is_flagged_for_review(rt):
    add_mock_model(rt, "only", script=[{"text": "not sure\nCONFIDENCE: 30"}])
    ladder = add_ladder(rt, "t", [{"model": "only", "min_confidence": 70}], triage=False)
    result, _ = await _run(rt, ladder)
    assert result.needs_review and result.text == "not sure"


async def test_draft_is_returned_flagged_when_higher_tiers_fail(rt):
    add_mock_model(rt, "small", script=[{"text": "draft answer\nCONFIDENCE: 50"}])
    add_mock_model(rt, "big", fail=True)
    ladder = add_ladder(rt, "t", [{"model": "small", "min_confidence": 80}, {"model": "big", "min_confidence": 0}],
                        triage=False)
    result, _ = await _run(rt, ladder)
    assert result.text == "draft answer" and result.needs_review and result.model_id == "small"


async def test_all_tiers_failing_raises(rt):
    add_mock_model(rt, "a", fail=True)
    add_mock_model(rt, "b", fail=True)
    ladder = add_ladder(rt, "t", [{"model": "a"}, {"model": "b"}], triage=False)
    with pytest.raises(LadderError):
        await _run(rt, ladder)
    with pytest.raises(LadderError, match="No model"):
        await _run(rt, {"id": "empty", "name": "empty", "tiers": [{"model": "missing"}], "options": {}})


async def test_high_risk_work_starts_at_the_floor_tier_and_is_reviewed(rt):
    # demo ladder: high_risk_min_tier=2, review=high_risk. A config change is classified high risk by the mock.
    result, events = await _run(rt, rt.db.get("ladders", "demo"), "Configure a new loopback please")
    assert result.model_id == "demo-medium"
    assert any(k == "review" and "approved" in s for k, s in events)


async def test_review_rejection_escalates(rt, monkeypatch):
    from netagent.models import mock

    add_mock_model(rt, "small", script=[{"text": "use telnet\nCONFIDENCE: 95"}])
    add_mock_model(rt, "big", script=[{"text": "use ssh\nCONFIDENCE: 95"}])
    ladder = add_ladder(rt, "t", [{"model": "small", "min_confidence": 70}, {"model": "big", "min_confidence": 0}],
                        triage=False, review="always")
    original = mock.MockProvider.chat

    async def chat(self, system, messages, tools=None, max_tokens=None, purpose="answer"):
        if purpose == "review":
            result = await original(self, system, messages, tools, max_tokens, purpose)
            result.text = "VERDICT: REJECT - telnet is insecure"
            return result
        return await original(self, system, messages, tools, max_tokens, purpose)

    monkeypatch.setattr(mock.MockProvider, "chat", chat)
    result, events = await _run(rt, ladder)
    assert result.text == "use ssh" and result.escalations == 1
    assert any(k == "escalation" and "rejected in review: telnet is insecure" in s for k, s in events)


async def test_cost_accounting(rt):
    add_mock_model(rt, "priced", cost_in=1_000_000, cost_out=0, script=[{"text": "x\nCONFIDENCE: 99"}])
    ladder = add_ladder(rt, "t", [{"model": "priced"}], triage=False)
    result, _ = await _run(rt, ladder)
    assert result.input_tokens > 0 and result.cost == pytest.approx(result.input_tokens)
