"""Queueing, routing, collaboration between agents and restart recovery."""
import asyncio

from conftest import add_agent, add_ladder, add_mock_model


async def _started(rt):
    await rt.orchestrator.start()
    return rt.orchestrator


async def test_routing_by_keyword_and_coordinator(rt):
    o = rt.orchestrator
    name = lambda agent_id: rt.db.get("agents", agent_id)["name"]  # noqa: E731
    assert name(o.route("Catalyst 9300 stack member down", "")) == "Cisco Expert"
    assert name(o.route("FortiGate policy not matching", "")) == "Fortinet Expert"
    assert name(o.route("MLAG peer-link problem", "on the Arista leafs")) == "Arista Expert"
    assert name(o.route("VPN between FortiGate and Cisco ASA", "")) == "Network Lead"  # tie -> coordinator
    assert name(o.route("Something completely unrelated", "")) == "Network Lead"
    assert name(o.route("ios upgrade", "eos is fine")) == "Network Lead"  # two vendors named
    assert name(o.route("videos about chaos engineering", "")) == "Network Lead"  # no substring false positives


async def test_task_runs_to_completion_with_timeline(rt):
    o = await _started(rt)
    try:
        task = o.create_task("What does ARP do?", "", requester="alice")
        done = await o.wait_for(task["id"], 10)
        assert done["status"] == "completed" and done["model_id"] == "demo-small" and done["result"]
        assert done["confidence"] == 92 and done["completed_at"] and done["input_tokens"] > 0
        kinds = [e["kind"] for e in rt.db.query("SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", [task["id"]])]
        assert kinds[0] == "status" and "triage" in kinds and "attempt" in kinds and kinds[-1] == "status"
    finally:
        await o.stop()


async def test_agents_consult_each_other(rt):
    o = await _started(rt)
    try:
        lead = rt.db.one("SELECT id FROM agents WHERE name = 'Network Lead'")["id"]
        task = o.create_task("Fortinet tunnel issue", "How do I check the FortiGate tunnel?", agent_id=lead)
        done = await o.wait_for(task["id"], 10)
        assert done["status"] == "completed"
        children = rt.db.query("SELECT * FROM tasks WHERE parent_id = ?", [task["id"]])
        assert len(children) == 1
        child = children[0]
        fortinet = rt.db.one("SELECT id FROM agents WHERE name = 'Fortinet Expert'")["id"]
        assert child["agent_id"] == fortinet and child["status"] == "completed" and child["depth"] == 1
        assert child["source"] == "agent" and child["requester"] == "Network Lead"
        assert f"task #{child['id']}" in done["result"]
    finally:
        await o.stop()


async def test_collaboration_guards(rt):
    from netagent.tools import ToolContext, run_tool, tools_for

    agent = rt.db.one("SELECT * FROM agents WHERE name = 'Network Lead'")
    profile = rt.db.get("profiles", agent["profile_id"])
    allowed = {t.spec.name for t in tools_for(profile["skills"])}
    task = rt.orchestrator.create_task("t", "t", agent_id=agent["id"], inline=True)
    ctx = ToolContext(rt=rt, agent=agent, profile=profile, task=task)

    out, err = await run_tool(ctx, "consult_agent", {"agent": "Network Lead", "question": "x"}, allowed)
    assert err and "yourself" in out
    out, err = await run_tool(ctx, "consult_agent", {"agent": "Nobody", "question": "x"}, allowed)
    assert err and "Cisco Expert" in out

    rt.db.update("agents", rt.db.one("SELECT id FROM agents WHERE name = 'Cisco Expert'")["id"], {"status": "paused"})
    out, err = await run_tool(ctx, "consult_agent", {"agent": "Cisco Expert", "question": "x"}, allowed)
    assert err and "paused" in out

    rt.db.update("tasks", task["id"], {"depth": 3})
    ctx.task = rt.db.get("tasks", task["id"])
    out, err = await run_tool(ctx, "consult_agent", {"agent": "Arista Expert", "question": "x"}, allowed)
    assert err and "depth limit" in out

    out, err = await run_tool(ctx, "check_task", {"task_id": 9999}, allowed)
    assert err and "not a sub-task" in out


async def test_delegate_and_check(rt):
    from netagent.tools import ToolContext, run_tool, tools_for

    o = await _started(rt)
    try:
        agent = rt.db.one("SELECT * FROM agents WHERE name = 'Network Lead'")
        profile = rt.db.get("profiles", agent["profile_id"])
        allowed = {t.spec.name for t in tools_for(profile["skills"])}
        task = o.create_task("parent", "parent", agent_id=agent["id"], inline=True)
        ctx = ToolContext(rt=rt, agent=agent, profile=profile, task=task)
        out, err = await run_tool(ctx, "delegate_task", {"agent": "Arista Expert", "title": "What does ARP do?",
                                                         "instructions": "Explain briefly."}, allowed)
        assert not err
        child = rt.db.one("SELECT * FROM tasks WHERE parent_id = ?", [task["id"]])
        out, err = await run_tool(ctx, "check_task", {"task_id": child["id"], "wait_seconds": 10}, allowed)
        assert not err and "completed" in out
    finally:
        await o.stop()


async def test_failure_when_no_model_available(make_rt):
    rt = make_rt(demo=False)  # default ladder: Ollama not running, no Anthropic key
    add_mock_model(rt, "dead", fail=True)
    add_ladder(rt, "dead", [{"model": "dead"}])
    o = await _started(rt)
    try:
        task = o.create_task("anything", "")
        done = await o.wait_for(task["id"], 10)
        assert done["status"] == "failed" and "No model tier could complete" in done["error"]
    finally:
        await o.stop()


async def test_paused_agent_holds_tasks_and_cancel(rt):
    o = await _started(rt)
    try:
        cisco = rt.db.one("SELECT id FROM agents WHERE name = 'Cisco Expert'")["id"]
        rt.db.update("agents", cisco, {"status": "paused"})
        task = o.create_task("What does ARP do?", "", agent_id=cisco)
        await asyncio.sleep(0.3)
        assert rt.db.get("tasks", task["id"])["status"] == "queued"
        assert o.cancel(task["id"]) and rt.db.get("tasks", task["id"])["status"] == "cancelled"
        assert not o.cancel(task["id"])
        again = o.retry(task["id"])
        assert again["status"] == "queued"
        rt.db.update("agents", cisco, {"status": "active"})
        o.kick()
        assert (await o.wait_for(task["id"], 10))["status"] == "completed"
    finally:
        await o.stop()


async def test_unassigned_tasks_wait_for_an_agent(make_rt):
    rt = make_rt(demo=True)
    rt.db.execute("DELETE FROM agents")
    o = await _started(rt)
    try:
        task = o.create_task("What does ARP do?", "")
        assert task["agent_id"] is None
        await asyncio.sleep(0.2)
        assert rt.db.get("tasks", task["id"])["status"] == "queued"
        add_agent(rt, "Solo", "general-assistant")
        o.kick()
        done = await o.wait_for(task["id"], 10)
        assert done["status"] == "completed" and done["agent_id"] is not None
    finally:
        await o.stop()


async def test_restart_recovery(rt):
    o = rt.orchestrator
    stuck = o.create_task("was running", "")
    rt.db.update("tasks", stuck["id"], {"status": "running"})
    inline = o.create_task("consult", "", inline=True, parent_id=stuck["id"])
    o._recover()
    assert rt.db.get("tasks", stuck["id"])["status"] == "queued"
    assert rt.db.get("tasks", inline["id"])["status"] == "failed"


async def test_thread_history_is_given_to_the_agent(rt):
    o = await _started(rt)
    try:
        first = o.create_task("What does ARP do?", "", thread_key="chat-1")
        await o.wait_for(first["id"], 10)
        second = o.create_task("And RARP?", "", thread_key="chat-1")
        message = o._task_message(rt.db.get("tasks", second["id"]))
        assert "Earlier in this conversation" in message and "What does ARP do?" in message
        other = o.create_task("unrelated", "", thread_key="chat-2")
        assert "Earlier in this conversation" not in o._task_message(rt.db.get("tasks", other["id"]))
    finally:
        await o.stop()


async def test_routing_spreads_load_across_agents_of_one_profile(rt):
    second = add_agent(rt, "Cisco Expert 2", "cisco-expert")
    first = rt.db.one("SELECT id FROM agents WHERE name = 'Cisco Expert'")["id"]
    assert rt.orchestrator.route("Catalyst trunk issue", "") == first
    rt.orchestrator.create_task("busy", "", agent_id=first)  # queued (dispatcher not started)
    assert rt.orchestrator.route("Catalyst trunk issue", "") == second["id"]
