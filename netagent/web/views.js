/* BlackHawk NetAgent console - pages. Each view fills `root`; assigning ctx.load makes it refresh on live events. */
"use strict";

function pageHead(title, subtitle, ...actions) {
  return h("div", { class: "page-head" }, h("div", {}, h("h1", {}, title), subtitle ? h("p", {}, subtitle) : null),
    h("div", { class: "actions" }, actions));
}
function table(headers, rows, emptyText) {
  if (!rows.length) return h("div", { class: "card empty" }, emptyText || "Nothing here yet.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, headers.map((c) => h("th", { class: typeof c === "object" ? c.class : "" }, typeof c === "object" ? c.label : c)))),
    h("tbody", {}, rows)));
}
function kpi(label, value, sub, cls) {
  return h("div", { class: "kpi " + (cls || "") }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    sub ? h("div", { class: "sub" }, sub) : null);
}
function ladderStrip(ladder) {
  return h("div", { class: "ladder" }, ladder.tiers.map((t, i) => h("div", { class: "tier" + (t.usable === false ? " off" : ""), title: t.status || "" },
    h("div", { class: "t-n" }, `Tier ${i + 1} `, t.location ? pill(t.location, t.location) : null),
    h("div", { class: "t-m" }, t.model_name || t.model),
    h("div", { class: "t-d" }, i === ladder.tiers.length - 1 ? "final tier"
      : `up to complexity ${t.max_complexity} · escalates below ${t.min_confidence}% confidence`),
    t.usable === false ? h("div", { class: "t-d" }, "unavailable: " + t.status) : null)));
}
async function agentOptions(includeAuto = true) {
  const agents = await api("/agents");
  return [...(includeAuto ? [["", "Auto-route to the best agent"]] : []), ...agents.map((a) => [a.id, `${a.name} - ${a.profile_name}`])];
}

async function newTaskDialog(presetAgent) {
  const form = buildForm([
    { key: "title", label: "Title", placeholder: "e.g. BGP neighbor flapping on leaf-dc1-01" },
    { key: "description", label: "Details", type: "textarea", rows: 8, placeholder: "Describe the request. Paste configuration or command output if you have it." },
    { key: "agent", label: "Assign to", type: "select", options: await agentOptions(), default: presetAgent || "", half: true },
    { key: "priority", label: "Priority", type: "select", options: [[1, "Urgent"], [2, "High"], [3, "Normal"], [4, "Low"]], default: 3, half: true },
  ]);
  modal({
    title: "New task", body: form.el,
    actions: [{ label: "Create task", run: async () => {
      const v = form.values();
      const task = await attempt(() => api("/tasks", { method: "POST", body: { ...v, agent: v.agent || null, priority: Number(v.priority) } }));
      if (!task) return false;
      location.hash = `#/tasks/${task.id}`;
    } }],
  });
}

// =================================================================== dashboard
views.dashboard = async (root, args, ctx) => {
  const kpis = h("div", { class: "kpis" });
  const agents = h("div", { class: "grid cols-3" });
  const feed = h("ul", { class: "timeline feed" });
  const ladder = h("div", {});
  fill(root, 
    pageHead("Dashboard", "Live view of your agents, their work and the model ladder.",
      h("button", { class: "btn primary", onclick: () => newTaskDialog() }, "New task")),
    kpis,
    h("div", { class: "card" }, h("h3", {}, "Escalation ladder"), ladder),
    h("div", { class: "section split" },
      h("div", {}, h("h3", {}, "Agents"), agents),
      h("div", { class: "card" }, h("h3", {}, "Recent activity"), feed)));

  ctx.load = async () => {
    const [o, ladders] = await Promise.all([api("/overview"), api("/ladders")]);
    const c = o.counts;
    fill(kpis, 
      kpi("Open tasks", c.queued + c.running, `${c.running} running · ${c.queued} queued`),
      kpi("Completed (24h)", c.completed_24h, `${c.completed} all time`),
      kpi("Failed (24h)", c.failed_24h, null, c.failed_24h ? "alert" : ""),
      kpi("Needs review (7d)", c.needs_review, "low-confidence answers", c.needs_review ? "warn" : ""),
      kpi("Pending approvals", c.pending_changes, "config change requests", c.pending_changes ? "warn" : ""),
      kpi("Models available", o.models_usable, "across local and cloud"));
    const current = ladders.find((l) => l.default) || ladders[0];
    fill(ladder, current
      ? h("div", {}, h("div", { class: "muted small", style: "margin-bottom:8px" }, `${current.name} - ${current.description}`), ladderStrip(current))
      : h("div", { class: "empty" }, "No ladder configured."));
    fill(agents, ...(o.agents.length ? o.agents.map((a) => h("div", { class: "card agent", style: `--agent:${a.color || "var(--accent)"}` },
      h("div", { class: "agent-head" },
        h("div", {}, h("div", { class: "agent-name" }, a.name), h("div", { class: "muted small" }, a.profile_name || a.profile_id)),
        pill(a.status)),
      h("div", { class: "now" }, a.current_task
        ? h("span", {}, "Working on ", h("a", { href: `#/tasks/${a.current_task.id}` }, `#${a.current_task.id} ${a.current_task.title}`))
        : h("span", { class: "muted" }, a.status === "paused" ? "Paused" : "Idle")),
      h("div", { class: "stats" }, h("span", {}, h("b", {}, a.running), " running"), h("span", {}, h("b", {}, a.queued), " queued"),
        h("span", {}, h("b", {}, a.completed), " done")),
      h("div", { class: "actions" }, h("a", { class: "btn sm", href: `#/chat/${a.id}` }, "Chat"),
        h("button", { class: "btn sm", onclick: () => newTaskDialog(a.id) }, "Assign task"))))
      : [h("div", { class: "card empty" }, "No agents yet. ", h("a", { href: "#/agents" }, "Spawn one"), ".")]));
    fill(feed, ...(o.activity.length ? o.activity.map((e) => h("li", {},
      h("span", { class: "ts" }, fmtTime(e.ts)), h("span", { class: `kind ${e.kind}` }, e.kind),
      h("span", { class: "sum" }, h("a", { href: `#/tasks/${e.task_id}` }, `#${e.task_id}`), " ", e.summary,
        h("div", { class: "title" }, e.title))))
      : [h("li", { class: "empty", style: "display:block" }, "No activity yet. Create a task to see the agents work.")]));
  };
  await ctx.load();
};

// ======================================================================= tasks
const taskFilters = { status: "", agent_id: "", source: "", q: "" };

views.tasks = async (root, args, ctx) => {
  if (args[0]) return taskDetail(root, Number(args[0]), ctx);
  const body = h("div", {});
  const agents = await api("/agents");
  const control = (key, el) => { el.value = taskFilters[key]; el.addEventListener("input", () => { taskFilters[key] = el.value; ctx.load(); }); return el; };
  fill(root, 
    pageHead("Tasks", "Everything the agents have been asked to do, from every channel.",
      h("a", { class: "btn", href: "/api/v1/reports/tasks.csv?days=90" }, "Export CSV"),
      h("button", { class: "btn primary", onclick: () => newTaskDialog() }, "New task")),
    h("div", { class: "filters" },
      control("status", h("select", { "aria-label": "Status" }, [["", "All statuses"], ["open", "Open"], ["queued", "Queued"], ["running", "Running"],
        ["completed", "Completed"], ["review", "Needs review"], ["failed", "Failed"], ["cancelled", "Cancelled"]].map(([v, t]) => h("option", { value: v }, t)))),
      control("agent_id", h("select", { "aria-label": "Agent" }, h("option", { value: "" }, "All agents"), agents.map((a) => h("option", { value: a.id }, a.name)))),
      control("source", h("select", { "aria-label": "Channel" }, [["", "All channels"], ["web", "Web"], ["api", "API"], ["teams", "Teams"], ["email", "Email"]]
        .map(([v, t]) => h("option", { value: v }, t)))),
      control("q", h("input", { type: "search", placeholder: "Search title, text, requester", "aria-label": "Search" }))),
    body);
  ctx.load = async () => {
    const qs = new URLSearchParams(Object.entries(taskFilters).filter(([, v]) => v !== ""));
    const tasks = await api("/tasks?" + qs.toString());
    fill(body, table(
      ["#", "Task", "Status", "Agent", "Answered by", { label: "Esc.", class: "num" }, "Channel", "Created", { label: "Run time", class: "num" }],
      tasks.map((t) => h("tr", { class: "click", onclick: () => { location.hash = `#/tasks/${t.id}`; } },
        h("td", { class: "nowrap muted" }, t.id),
        h("td", {}, h("div", { class: "title-cell" }, t.title), t.children ? h("span", { class: "tag" }, `${t.children} sub-task${t.children > 1 ? "s" : ""}`) : null,
          t.priority < 3 ? h("span", { class: "tag" }, PRIORITY[t.priority]) : null),
        h("td", {}, statusPill(t)), h("td", { class: "nowrap" }, t.agent_name || h("span", { class: "muted" }, "unassigned")),
        h("td", { class: "nowrap" }, t.model_name ? [t.model_name, " ", pill(t.model_location, t.model_location)] : h("span", { class: "muted" }, "-")),
        h("td", { class: "num" }, t.escalations || ""), h("td", {}, t.source), h("td", { class: "nowrap", title: t.created_at }, ago(t.created_at)),
        h("td", { class: "num nowrap" }, fmtDur(t.seconds)))),
      "No tasks match. Create one with New task, or send one in through Teams, email or the API."));
  };
  await ctx.load();
};

async function taskDetail(root, id, ctx) {
  const box = h("div", {});
  fill(root, box);
  ctx.load = async () => {
    const t = await api(`/tasks/${id}`);
    const open = t.status === "queued" || t.status === "running";
    const trail = t.events.filter((e) => ["triage", "attempt", "escalation", "review", "ladder"].includes(e.kind));
    fill(box, 
      pageHead(`#${t.id} ${t.title}`, null,
        h("a", { class: "btn", href: "#/tasks" }, "Back to tasks"),
        h("a", { class: "btn", href: `/api/v1/tasks/${t.id}/report`, target: "_blank" }, "Report"),
        open ? h("button", { class: "btn danger", onclick: async () => { if (await attempt(() => api(`/tasks/${id}/cancel`, { method: "POST" }), "Cancelling")) ctx.load(); } }, "Cancel")
          : h("button", { class: "btn", onclick: async () => { if (await attempt(() => api(`/tasks/${id}/retry`, { method: "POST" }), "Re-queued")) ctx.load(); } }, "Run again")),
      t.status === "completed" && t.needs_review ? h("div", { class: "callout warn" },
        "Low confidence: no model tier was sure of this answer. Have an engineer review it before acting on it.") : null,
      t.status === "failed" ? h("div", { class: "callout bad" }, "Failed: " + (t.error || "unknown error")) : null,
      h("div", { class: "card" }, h("div", { class: "meta" },
        h("div", {}, h("span", {}, "Status"), statusPill(t)),
        h("div", {}, h("span", {}, "Agent"), t.agent_name || "unassigned"),
        h("div", {}, h("span", {}, "Answered by"), t.model_name ? [t.model_name, " ", pill(t.model_location, t.model_location)] : "-"),
        h("div", {}, h("span", {}, "Escalations"), t.escalations),
        h("div", {}, h("span", {}, "Confidence"), t.confidence === null ? "-" : `${t.confidence}%`),
        h("div", {}, h("span", {}, "Requested by"), `${t.requester || "-"} via ${t.source}`),
        h("div", {}, h("span", {}, "Priority"), PRIORITY[t.priority]),
        h("div", {}, h("span", {}, "Created"), fmtTime(t.created_at)),
        h("div", {}, h("span", {}, "Run time"), open && t.started_at ? "running" : fmtDur(t.seconds)),
        h("div", {}, h("span", {}, "Tokens / est. cost"), `${t.input_tokens + t.output_tokens} / $${(t.cost || 0).toFixed(4)}`),
        t.parent ? h("div", {}, h("span", {}, "Parent task"), h("a", { href: `#/tasks/${t.parent.id}` }, `#${t.parent.id} ${t.parent.title}`)) : null)),
      h("div", { class: "section split" },
        h("div", { class: "grid" },
          h("div", { class: "card" }, h("h3", {}, "Result"),
            t.result ? h("div", { class: "md", html: md(t.result) })
              : h("div", { class: "empty" }, open ? "The agent is working on it. This page updates live." : "No result.")),
          t.description && t.description !== t.title ? h("div", { class: "card" }, h("h3", {}, "Request"), h("div", { class: "md", html: md(t.description) })) : null,
          t.subtasks.length ? h("div", {}, h("h3", {}, "Sub-tasks (agent collaboration)"), table(["#", "Task", "Agent", "Status"],
            t.subtasks.map((s) => h("tr", { class: "click", onclick: () => { location.hash = `#/tasks/${s.id}`; } },
              h("td", { class: "muted" }, s.id), h("td", {}, h("div", { class: "title-cell" }, s.title)), h("td", {}, s.agent_name || "-"), h("td", {}, statusPill(s)))))) : null,
          t.changes.length ? h("div", {}, h("h3", {}, "Change requests"), table(["CR", "Device", "Summary", "Risk", "Status"],
            t.changes.map((c) => h("tr", { class: "click", onclick: () => { location.hash = "#/approvals"; } },
              h("td", { class: "muted" }, `CR-${c.id}`), h("td", {}, c.device), h("td", {}, c.summary), h("td", {}, pill(c.risk)), h("td", {}, pill(c.status)))))) : null),
        h("div", { class: "grid" },
          trail.length ? h("div", { class: "card" }, h("h3", {}, "Escalation trail"),
            h("ul", { class: "timeline" }, trail.map((e) => h("li", { style: "grid-template-columns: 76px 1fr" },
              h("span", { class: `kind ${e.kind}` }, e.kind), h("span", { class: "sum" }, e.summary))))) : null,
          h("div", { class: "card" }, h("h3", {}, "Timeline"),
            h("ul", { class: "timeline" }, t.events.map((e) => h("li", {}, h("span", { class: "ts" }, fmtTime(e.ts)),
              h("span", { class: `kind ${e.kind}` }, e.kind.replace("_", " ")), h("span", { class: "sum" }, e.summary))))))));
  };
  await ctx.load();
}

// ====================================================================== agents
views.agents = async (root, args, ctx) => {
  const grid = h("div", { class: "grid cols-3" });
  fill(root, 
    pageHead("Agents", "Spawn agents from profiles. Each agent has its own queue and can consult or delegate to the others.",
      h("button", { class: "btn primary", onclick: () => spawnDialog(ctx) }, "Spawn agent")),
    grid);
  ctx.load = async () => {
    const agents = await api("/agents");
    fill(grid, ...(agents.length ? agents.map((a) => h("div", { class: "card agent", style: `--agent:${a.color || "var(--accent)"}` },
      h("div", { class: "agent-head" },
        h("div", {}, h("div", { class: "agent-name" }, a.name),
          h("div", { class: "muted small" }, a.profile_name || `missing profile ${a.profile_id}`, a.coordinator ? " · coordinator" : "")),
        pill(a.status)),
      h("div", { class: "small muted" }, a.profile_description || ""),
      h("div", {}, (a.skills || []).map((s) => h("span", { class: "tag" }, s))),
      h("div", { class: "small" }, h("span", { class: "muted" }, "Ladder: "), a.effective_ladder || "none", a.ladder_id ? " (override)" : ""),
      h("div", { class: "stats" }, h("span", {}, h("b", {}, a.open), " open"), h("span", {}, h("b", {}, a.completed), " completed"),
        h("span", {}, h("b", {}, a.failed), " failed"), h("span", {}, h("b", {}, a.max_parallel), " parallel")),
      h("div", { class: "actions" },
        h("a", { class: "btn sm", href: `#/chat/${a.id}` }, "Chat"),
        h("button", { class: "btn sm", onclick: () => newTaskDialog(a.id) }, "Assign task"),
        h("button", { class: "btn sm", onclick: async () => {
          if (await attempt(() => api(`/agents/${a.id}`, { method: "PATCH", body: { status: a.status === "active" ? "paused" : "active" } }))) ctx.load();
        } }, a.status === "active" ? "Pause" : "Resume"),
        h("button", { class: "btn sm", onclick: () => editAgentDialog(a, ctx) }, "Edit"),
        h("button", { class: "btn sm danger", onclick: async () => {
          if (!confirm(`Delete agent "${a.name}"? Running tasks are cancelled; queued tasks are re-routed.`)) return;
          if (await attempt(() => api(`/agents/${a.id}`, { method: "DELETE" }), "Agent deleted")) ctx.load();
        } }, "Delete"))))
      : [h("div", { class: "card empty" }, "No agents. Spawn one from a profile.")]));
  };
  await ctx.load();
};

async function spawnDialog(ctx) {
  const [profiles, ladders] = await Promise.all([api("/profiles"), api("/ladders")]);
  const name = h("input", { type: "text", placeholder: "e.g. Cisco Expert 2" });
  const picker = h("div", { class: "profile-pick" }, profiles.map((p, i) => h("label", { class: "profile-opt", style: `--agent:${p.color || "var(--border)"}` },
    h("input", { type: "radio", name: "profile", value: p.id, checked: i === 0, onchange: () => { if (!name.dataset.touched) name.value = suggest(p); } }),
    h("div", { class: "n" }, p.name), h("div", { class: "d" }, p.description),
    h("div", { style: "margin-top:6px" }, p.skills.map((s) => h("span", { class: "tag" }, s))))));
  const suggest = (p) => p.name.replace(/\s*\(.*\)$/, "");
  name.addEventListener("input", () => { name.dataset.touched = "1"; });
  if (profiles[0]) name.value = suggest(profiles[0]);
  const extra = buildForm([
    { key: "ladder_id", label: "Model ladder", type: "select", half: true,
      options: [["", "Use the profile / default ladder"], ...ladders.map((l) => [l.id, l.name])] },
    { key: "max_parallel", label: "Tasks in parallel", type: "number", default: 2, half: true },
  ]);
  modal({
    title: "Spawn agent", wide: true,
    body: h("div", { class: "form" }, h("div", {}, h("h3", {}, "1. Choose a profile"), picker),
      h("label", {}, "2. Agent name", name), extra.el),
    actions: [{ label: "Spawn agent", run: async () => {
      const chosen = picker.querySelector("input:checked");
      const v = extra.values();
      const ok = await attempt(() => api("/agents", { method: "POST", body: {
        name: name.value, profile_id: chosen && chosen.value, ladder_id: v.ladder_id, max_parallel: v.max_parallel || 2 } }), "Agent spawned");
      if (!ok) return false;
      ctx.load();
    } }],
  });
}

async function editAgentDialog(agent, ctx) {
  const [profiles, ladders] = await Promise.all([api("/profiles"), api("/ladders")]);
  const form = buildForm([
    { key: "name", label: "Name" },
    { key: "profile_id", label: "Profile", type: "select", options: profiles.map((p) => [p.id, p.name]) },
    { key: "ladder_id", label: "Model ladder", type: "select", half: true, options: [["", "Use the profile / default ladder"], ...ladders.map((l) => [l.id, l.name])] },
    { key: "max_parallel", label: "Tasks in parallel", type: "number", half: true },
    { key: "notes", label: "Notes", type: "textarea", rows: 2 },
  ], agent);
  modal({ title: `Edit ${agent.name}`, body: form.el, actions: [{ label: "Save", run: async () => {
    const v = form.values();
    if (!await attempt(() => api(`/agents/${agent.id}`, { method: "PATCH", body: { ...v, max_parallel: v.max_parallel || 1 } }), "Saved")) return false;
    ctx.load();
  } }] });
}

// ======================================================================== chat
function chatThread(agentId, reset) {
  const key = `netagent.chat.${agentId}`;
  let id = null;
  try { id = reset ? null : localStorage.getItem(key); } catch (err) { /* ignore */ }
  if (!id) {
    id = `web:${agentId}:${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    try { localStorage.setItem(key, id); } catch (err) { /* ignore */ }
  }
  return id;
}

views.chat = async (root, args, ctx) => {
  const agents = await api("/agents");
  if (!agents.length) {
    fill(root, pageHead("Chat"), h("div", { class: "card empty" }, "No agents yet. ", h("a", { href: "#/agents" }, "Spawn one"), " to start chatting."));
    return;
  }
  const agent = agents.find((a) => String(a.id) === args[0]) || agents[0];
  let thread = chatThread(agent.id);
  const log = h("div", { class: "chat-log" });
  const input = h("textarea", { placeholder: `Message ${agent.name}... (Enter to send, Shift+Enter for a new line)`, "aria-label": "Message" });
  const send = async () => {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    if (await attempt(() => api("/tasks", { method: "POST", body: { description: text, agent: agent.id, thread_key: thread } }))) ctx.load(true);
  };
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); } });
  fill(root, 
    pageHead("Chat", "Talk to an agent directly. Every message is tracked as a task; the conversation carries context."),
    h("div", { class: "chat" },
      h("div", { class: "card chat-agents" }, agents.map((a) => h("button", {
        class: a.id === agent.id ? "active" : "", style: `--agent:${a.color || "transparent"}`, onclick: () => { location.hash = `#/chat/${a.id}`; },
      }, a.name, h("span", { class: "d" }, a.profile_name)))),
      h("div", { class: "card chat-main" },
        h("div", { style: "display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--border)" },
          h("div", {}, h("b", {}, agent.name), " ", pill(agent.status), h("div", { class: "muted small" }, agent.profile_description)),
          h("button", { class: "btn sm", style: "flex:none", onclick: () => { thread = chatThread(agent.id, true); ctx.load(true); } }, "New conversation")),
        log,
        h("div", { class: "chat-input" }, input, h("button", { class: "btn primary", onclick: send }, "Send")))));
  let lastSignature = "";
  ctx.load = async (scroll) => {
    const tasks = (await api(`/tasks?full=true&limit=60&thread_key=${encodeURIComponent(thread)}`)).reverse();
    const signature = tasks.map((t) => `${t.id}:${t.status}`).join(",");
    if (signature === lastSignature && !scroll) return;
    lastSignature = signature;
    fill(log, ...(tasks.length ? tasks.flatMap((t) => [
      h("div", { class: "msg user" }, t.description || t.title),
      h("div", { class: "msg agent" },
        t.status === "completed" ? h("div", { class: "md", html: md(t.result) })
          : t.status === "failed" ? h("div", { class: "error" }, "Failed: " + t.error)
            : t.status === "cancelled" ? h("span", { class: "muted" }, "Cancelled")
              : h("span", {}, pill(t.status), " working on it..."),
        h("div", { class: "foot" }, h("a", { href: `#/tasks/${t.id}` }, `Task #${t.id}`),
          t.model_name ? ` · ${t.model_name}` : "", t.escalations ? ` · ${t.escalations} escalation(s)` : "",
          t.needs_review ? " · low confidence, review before use" : "")),
    ]) : [h("div", { class: "empty" }, `Ask ${agent.name} a question, paste a config for review, or describe a problem.`)]));
    log.scrollTop = log.scrollHeight;
  };
  await ctx.load(true);
  input.focus();
};

// =================================================================== approvals
views.approvals = async (root, args, ctx) => {
  const pending = h("div", { class: "grid" });
  const history = h("div", {});
  fill(root, 
    pageHead("Approvals", "Agents cannot change devices on their own. Review each proposed change; it is pushed only when you approve."),
    h("h3", {}, "Waiting for a decision"), pending, h("div", { class: "section" }, h("h3", {}, "History"), history));
  ctx.load = async () => {
    const changes = await api("/changes");
    const waiting = changes.filter((c) => c.status === "pending");
    fill(pending, ...(waiting.length ? waiting.map((c) => {
      const save = h("input", { type: "checkbox" });
      return h("div", { class: "card" },
        h("div", { class: "agent-head" },
          h("div", {}, h("div", { class: "agent-name" }, `CR-${c.id} · ${c.summary}`),
            h("div", { class: "muted small" }, `${c.device} (${c.platform}) · proposed by ${c.agent || "?"} · `,
              c.task_id ? h("a", { href: `#/tasks/${c.task_id}` }, `task #${c.task_id}`) : "", ` · ${ago(c.created_at)}`)),
          pill(`${c.risk} risk`, c.risk)),
        c.rationale ? h("p", {}, c.rationale) : null,
        h("div", { class: "grid cols-2" },
          h("div", {}, h("h3", {}, "Commands to push"), h("pre", {}, c.commands)),
          h("div", {}, h("h3", {}, "Rollback"), h("pre", {}, c.rollback || "(none provided)"),
            c.verify ? [h("h3", {}, "Verification"), h("pre", {}, c.verify)] : null)),
        c.allow_config ? null : h("div", { class: "callout warn" },
          `Pushes are disabled for ${c.device}. Enable "Allow configuration changes" on the device, or reject and apply it manually.`),
        h("div", { class: "actions" },
          h("button", { class: "btn primary", disabled: !c.allow_config, onclick: async () => {
            if (!confirm(`Push ${c.commands.split("\n").length} line(s) to ${c.device}?`)) return;
            if (await attempt(() => api(`/changes/${c.id}/approve`, { method: "POST", body: { save_config: save.checked } }), "Approved - pushing to the device")) ctx.load();
          } }, "Approve and push"),
          h("button", { class: "btn danger", onclick: async () => {
            const note = prompt("Reason for rejecting (optional):", "");
            if (note === null) return;
            if (await attempt(() => api(`/changes/${c.id}/reject`, { method: "POST", body: { note } }), "Rejected")) ctx.load();
          } }, "Reject"),
          h("label", { class: "check" }, save, "Save to startup config after pushing")));
    }) : [h("div", { class: "card empty" }, "Nothing is waiting for approval.")]));
    const done = changes.filter((c) => c.status !== "pending");
    fill(history, table(["CR", "Device", "Summary", "Status", "Decided by", "When", ""],
      done.map((c) => h("tr", {}, h("td", { class: "muted nowrap" }, `CR-${c.id}`), h("td", {}, c.device), h("td", {}, c.summary),
        h("td", {}, pill(c.status)), h("td", {}, c.decided_by || "-"), h("td", { class: "nowrap" }, ago(c.decided_at)),
        h("td", {}, h("button", { class: "btn sm", onclick: () => modal({ title: `CR-${c.id} · ${c.summary}`, wide: true, body: h("div", {},
          c.decision_note ? h("p", {}, "Note: " + c.decision_note) : null,
          h("h3", {}, "Commands"), h("pre", {}, c.commands), h("h3", {}, "Device output"), h("pre", {}, c.output || "(none)"),
          c.backup ? [h("h3", {}, "Pre-change configuration backup"), h("pre", {}, c.backup)] : null) }) }, "Details")))),
      "No decisions yet."));
  };
  await ctx.load();
};

// ==================================================================== profiles
views.profiles = async (root, args, ctx) => {
  const grid = h("div", { class: "grid cols-3" });
  fill(root, 
    pageHead("Profiles", "A profile defines an agent's personality, brief, skills and knowledge. Spawn as many agents from a profile as you need.",
      h("button", { class: "btn primary", onclick: () => profileDialog(null, ctx) }, "New profile")),
    grid);
  ctx.load = async () => {
    const profiles = await api("/profiles");
    fill(grid, ...profiles.map((p) => h("div", { class: "card agent", style: `--agent:${p.color || "var(--accent)"}` },
      h("div", { class: "agent-head" }, h("div", { class: "agent-name" }, p.name),
        h("div", {}, p.builtin ? pill("built-in", "") : pill("custom", "ok"), p.coordinator ? [" ", pill("coordinator", "cloud")] : null)),
      h("div", { class: "small muted" }, p.description),
      h("div", {}, p.skills.map((s) => h("span", { class: "tag" }, s)), p.knowledge.map((k) => h("span", { class: "tag" }, `kb:${k}`))),
      h("div", { class: "small muted" }, `${p.agents} agent(s) · ${p.tools.length} tools`),
      h("div", { class: "actions" },
        h("button", { class: "btn sm", onclick: () => profileDialog(p, ctx, !p.builtin) }, p.builtin ? "View" : "Edit"),
        h("button", { class: "btn sm", onclick: () => profileDialog({ ...p, name: p.name + " (copy)", builtin: false, id: null }, ctx, true) }, "Duplicate"),
        p.builtin ? null : h("button", { class: "btn sm danger", onclick: async () => {
          if (!confirm(`Delete profile "${p.name}"?`)) return;
          if (await attempt(() => api(`/profiles/${p.id}`, { method: "DELETE" }), "Profile deleted")) ctx.load();
        } }, "Delete")))));
  };
  await ctx.load();
};

async function profileDialog(profile, ctx, editable = true) {
  const [catalog, ladders] = await Promise.all([api("/skills"), api("/ladders")]);
  const p = profile || { skills: ["network-core", "collaboration"], knowledge: ["common"], keywords: [], color: "#0047ab" };
  const form = buildForm([
    { key: "name", label: "Profile name", placeholder: "e.g. Wireless Engineer" },
    { key: "description", label: "One-line description", help: "Shown to people and to other agents deciding whom to consult." },
    { key: "personality", label: "Personality and tone", type: "textarea", rows: 3, placeholder: "How this agent sounds and behaves." },
    { key: "system_prompt", label: "Brief (instructions)", type: "textarea", rows: 9, placeholder: "What this agent is responsible for and how it should approach the work." },
    { key: "keywords", label: "Routing keywords", help: "Comma separated. Unaddressed tasks containing these words are routed to agents with this profile." },
    { key: "ladder_id", label: "Model ladder", type: "select", half: true, options: [["", "Default ladder"], ...ladders.map((l) => [l.id, l.name])] },
    { key: "color", label: "Colour", half: true, placeholder: "#0047ab" },
    { key: "coordinator", label: "Coordinator", type: "bool", help: "receives tasks that match no specialist" },
  ], { ...p, keywords: (p.keywords || []).join(", ") });
  const boxes = (items, chosen, describe) => items.map((item) => {
    const input = h("input", { type: "checkbox", value: item.id, checked: chosen.includes(item.id) });
    return h("label", { class: "check" }, input, h("span", {}, h("b", {}, item.name || item.id), h("span", { class: "help" }, " - " + describe(item))));
  });
  const skills = h("div", { class: "form" }, boxes(catalog.skills.filter((s) => s.id !== "task-tracking"), p.skills || [],
    (s) => `${s.description} Tools: ${s.tools.join(", ")}`));
  const knowledge = h("div", { class: "form" }, boxes(catalog.knowledge, p.knowledge || [], (k) => `playbooks: ${k.playbooks.join(", ") || "none"}`));
  const body = h("div", { class: "form" }, form.el, h("div", {}, h("h3", {}, "Skills"), skills),
    h("div", {}, h("h3", {}, "Knowledge packs (playbook folders)"), knowledge));
  if (!editable) body.querySelectorAll("input, textarea, select").forEach((el) => { el.disabled = true; });
  const picked = (el) => [...el.querySelectorAll("input:checked")].map((i) => i.value);
  modal({
    title: !editable ? p.name : p.id ? `Edit ${p.name}` : "New profile", wide: true, body,
    actions: editable ? [{ label: "Save profile", run: async () => {
      const v = form.values();
      const payload = { ...v, keywords: v.keywords.split(",").map((k) => k.trim()).filter(Boolean), skills: picked(skills), knowledge: picked(knowledge) };
      const ok = await attempt(() => (p.id ? api(`/profiles/${p.id}`, { method: "PUT", body: payload }) : api("/profiles", { method: "POST", body: payload })), "Profile saved");
      if (!ok) return false;
      ctx.load();
    } }] : [],
  });
}

// ============================================================ models & ladders
views.models = async (root, args, ctx) => {
  const modelsBox = h("div", {});
  const laddersBox = h("div", { class: "grid" });
  const settingsBox = h("div", { class: "card" });
  let providers = [];
  let models = [];
  fill(root, 
    pageHead("Models & Ladders", "Register local and cloud models, then arrange them into escalation ladders: small and fast first, larger models on demand.",
      h("button", { class: "btn", onclick: () => modelDialog(null, providers, ctx) }, "Add model"),
      h("button", { class: "btn primary", onclick: () => ladderDialog(null, models, ctx) }, "New ladder")),
    h("h3", {}, "Escalation ladders"), laddersBox,
    h("div", { class: "section" }, h("h3", {}, "Models"), modelsBox),
    h("div", { class: "section" }, h("h3", {}, "Agent run settings"), settingsBox));
  const load = async () => {
    const [m, ladders, settings] = await Promise.all([api("/models"), api("/ladders"), api("/settings")]);
    providers = m.providers;
    models = m.models;
    fill(laddersBox, ...ladders.map((l) => h("div", { class: "card" },
      h("div", { class: "agent-head", style: "margin-bottom:10px" },
        h("div", {}, h("span", { class: "agent-name" }, l.name), " ", l.default ? pill("default", "ok") : null, h("div", { class: "muted small" }, l.description)),
        h("div", { class: "actions" },
          l.default ? null : h("button", { class: "btn sm", onclick: async () => {
            if (await attempt(() => api("/settings", { method: "PUT", body: { default_ladder: l.id } }), "Default ladder changed")) load();
          } }, "Make default"),
          h("button", { class: "btn sm", onclick: () => ladderDialog(l, models, ctx) }, "Edit"),
          l.default ? null : h("button", { class: "btn sm danger", onclick: async () => {
            if (!confirm(`Delete ladder "${l.name}"?`)) return;
            if (await attempt(() => api(`/ladders/${l.id}`, { method: "DELETE" }), "Ladder deleted")) load();
          } }, "Delete"))),
      ladderStrip(l),
      h("div", { class: "muted small", style: "margin-top:8px" },
        `Triage: ${l.options.triage === false ? "off" : "on"} · Review by next tier: ${String(l.options.review || "high_risk").replace("_", " ")} · `
        + `High-risk work starts at tier ${l.options.high_risk_min_tier || 1} or above`))));
    fill(modelsBox, table(["Model", "Provider", "Model ID", "Where", "Status", { label: "$ / M tokens in · out", class: "num" }, ""],
      models.map((x) => h("tr", {},
        h("td", {}, h("b", {}, x.name), h("div", { class: "muted small mono" }, x.id)), h("td", {}, x.provider),
        h("td", { class: "mono" }, x.model, x.base_url ? h("div", { class: "muted small" }, x.base_url) : null),
        h("td", {}, pill(x.location, x.location)),
        h("td", {}, x.usable ? pill("ready", "ok") : pill(x.enabled ? "unavailable" : "disabled", x.enabled ? "pending" : ""),
          x.usable ? null : h("div", { class: "muted small" }, x.status)),
        h("td", { class: "num nowrap" }, x.cost_in || x.cost_out ? `${x.cost_in} · ${x.cost_out}` : "-"),
        h("td", { class: "nowrap" },
          h("button", { class: "btn sm", onclick: async (ev) => {
            ev.target.disabled = true; ev.target.textContent = "Testing...";
            const r = await attempt(() => api(`/models/${x.id}/test`, { method: "POST" }));
            if (r) toast(`${x.name}: ${r.message}`, !r.ok);
            load();
          } }, "Test"), " ",
          h("button", { class: "btn sm", onclick: () => modelDialog(x, providers, ctx) }, "Edit"), " ",
          h("button", { class: "btn sm danger", onclick: async () => {
            if (!confirm(`Delete model "${x.name}"?`)) return;
            if (await attempt(() => api(`/models/${x.id}`, { method: "DELETE" }), "Model deleted")) load();
          } }, "Delete")))),
      "No models registered."));
    const form = buildForm([
      { key: "redact_secrets", label: "Redact credentials in device output before any model sees it", type: "bool" },
      { key: "max_steps", label: "Maximum tool steps per model attempt", type: "number", half: true },
      { key: "max_collab_depth", label: "Maximum agent-to-agent delegation depth", type: "number", half: true },
    ], settings);
    fill(settingsBox, form.el, h("div", { class: "actions", style: "margin-top:12px" },
      h("button", { class: "btn", onclick: () => attempt(() => api("/settings", { method: "PUT", body: form.values() }), "Settings saved") }, "Save settings")));
  };
  await load(); // not assigned to ctx.load: this page has forms that must not re-render on live events
};

function modelDialog(model, providers, ctx) {
  const m = model || { provider: "ollama", location: "local", enabled: true, options: {} };
  const form = buildForm([
    { key: "name", label: "Display name", placeholder: "e.g. Local Llama 3.2", half: true },
    { key: "provider", label: "Provider", type: "select", options: providers.map((p) => [p.id, p.name]), half: true },
    { key: "model", label: "Model ID", placeholder: "llama3.2:3b · claude-sonnet-5-5 · ...", half: true },
    { key: "location", label: "Runs", type: "select", options: [["local", "Locally (data stays on your network)"], ["cloud", "In the cloud"]], half: true },
    { key: "base_url", label: "Base URL", help: "Required for local runtimes and gateways. Leave empty for the provider's default.", placeholder: "http://localhost:11434" },
    { key: "api_key_env", label: "API key: environment variable", placeholder: "ANTHROPIC_API_KEY", half: true },
    { key: "api_key", label: model && model.has_key ? "API key (stored - leave blank to keep)" : "API key (stored encrypted)", type: "secret", half: true },
    { key: "cost_in", label: "Cost per million input tokens ($)", type: "number", step: "0.01", half: true },
    { key: "cost_out", label: "Cost per million output tokens ($)", type: "number", step: "0.01", half: true },
    { key: "options", label: "Options (JSON)", type: "textarea", rows: 3,
      help: 'e.g. {"effort": "medium"} for Claude, {"supports_tools": false, "timeout": 300} for small local models.' },
    { key: "enabled", label: "Enabled", type: "bool" },
  ], { ...m, options: JSON.stringify(m.options || {}) });
  modal({ title: model ? `Edit ${model.name}` : "Add model", body: form.el, actions: [{ label: "Save model", run: async () => {
    const v = form.values();
    let options;
    try { options = JSON.parse(v.options || "{}"); } catch (err) { toast("Options must be valid JSON", true); return false; }
    const body = { ...v, options, cost_in: v.cost_in || 0, cost_out: v.cost_out || 0, api_key: v.api_key === "" ? null : v.api_key };
    const ok = await attempt(() => (model ? api(`/models/${model.id}`, { method: "PUT", body }) : api("/models", { method: "POST", body })), "Model saved");
    if (!ok) return false;
    location.hash === "#/models" ? route() : (location.hash = "#/models");
  } }] });
}

function ladderDialog(ladder, models, ctx) {
  const l = ladder || { name: "", description: "", tiers: [], options: { triage: true, review: "high_risk", high_risk_min_tier: 1 } };
  const head = buildForm([{ key: "name", label: "Ladder name" }, { key: "description", label: "Description" }], l);
  const rows = h("div", { class: "form" });
  const addTier = (tier = {}) => {
    const select = h("select", {}, models.map((m) => h("option", { value: m.id, selected: m.id === tier.model }, `${m.name} (${m.location})`)));
    const complexity = h("input", { type: "number", min: 1, max: 5, value: tier.max_complexity ?? 5 });
    const confidence = h("input", { type: "number", min: 0, max: 100, value: tier.min_confidence ?? 70 });
    const row = h("div", { class: "row3", style: "align-items:end;grid-template-columns:2fr 1fr 1fr auto" },
      h("label", {}, "Model", select), h("label", {}, "Starts tasks up to complexity", complexity),
      h("label", {}, "Escalate below confidence %", confidence),
      h("button", { class: "btn sm danger", type: "button", onclick: () => row.remove() }, "Remove"));
    row.read = () => ({ model: select.value, max_complexity: Number(complexity.value), min_confidence: Number(confidence.value) });
    rows.append(row);
  };
  l.tiers.forEach(addTier);
  if (!l.tiers.length) addTier();
  const opts = buildForm([
    { key: "triage", label: "Triage each task with the smallest model first", type: "bool" },
    { key: "review", label: "Have the next tier review answers", type: "select", half: true,
      options: [["never", "Never"], ["high_risk", "For high-risk work and config changes"], ["always", "Always"]] },
    { key: "high_risk_min_tier", label: "High-risk work starts at tier", type: "number", half: true },
  ], l.options);
  modal({
    title: ladder ? `Edit ${ladder.name}` : "New ladder", wide: true,
    body: h("div", { class: "form" }, head.el,
      h("div", {}, h("h3", {}, "Tiers - cheapest and fastest first"), rows,
        h("button", { class: "btn sm", type: "button", style: "margin-top:8px", onclick: () => addTier() }, "Add tier")),
      h("div", {}, h("h3", {}, "Behaviour"), opts.el)),
    actions: [{ label: "Save ladder", run: async () => {
      const body = { ...head.values(), tiers: [...rows.children].map((r) => r.read()), options: opts.values() };
      const ok = await attempt(() => (ladder ? api(`/ladders/${ladder.id}`, { method: "PUT", body }) : api("/ladders", { method: "POST", body })), "Ladder saved");
      if (!ok) return false;
      route();
    } }],
  });
}

// ===================================================================== devices
views.devices = async (root, args, ctx) => {
  const box = h("div", {});
  let platforms = {};
  fill(root, 
    pageHead("Devices", "The inventory agents can reach. Agents run read-only commands themselves; configuration pushes always need your approval.",
      h("button", { class: "btn primary", onclick: () => deviceDialog(null, platforms, ctx) }, "Add device")),
    box);
  ctx.load = async () => {
    const data = await api("/devices");
    platforms = data.platforms;
    fill(box, table(["Name", "Platform", "Address", "Site", "Tags", "Config changes", ""],
      data.devices.map((d) => h("tr", {},
        h("td", {}, h("b", {}, d.name)), h("td", {}, d.platform_name),
        h("td", { class: "mono" }, d.transport === "simulated" ? pill("simulated", "") : `${d.host}:${d.port}`), h("td", {}, d.site || "-"),
        h("td", {}, (d.tags || "").split(",").filter(Boolean).map((t) => h("span", { class: "tag" }, t.trim()))),
        h("td", {}, d.allow_config ? pill("with approval", "pending") : pill("blocked", "")),
        h("td", { class: "nowrap" },
          h("button", { class: "btn sm", onclick: async (ev) => {
            ev.target.disabled = true; ev.target.textContent = "Testing...";
            const r = await attempt(() => api(`/devices/${d.id}/test`, { method: "POST" }));
            if (r) toast(`${d.name}: ${r.message}`, !r.ok);
            ev.target.disabled = false; ev.target.textContent = "Test";
          } }, "Test"), " ",
          h("button", { class: "btn sm", onclick: () => deviceDialog(d, platforms, ctx) }, "Edit"), " ",
          h("button", { class: "btn sm danger", onclick: async () => {
            if (!confirm(`Remove ${d.name} from the inventory?`)) return;
            if (await attempt(() => api(`/devices/${d.id}`, { method: "DELETE" }), "Device removed")) ctx.load();
          } }, "Delete")))),
      "No devices yet. Add your Cisco, Fortinet and Arista devices so agents can gather evidence themselves."));
  };
  await ctx.load();
};

function deviceDialog(device, platforms, ctx) {
  const d = device || { port: 22, transport: "ssh", platform: "cisco_xe" };
  const form = buildForm([
    { key: "name", label: "Name", placeholder: "sw-core-01", half: true },
    { key: "platform", label: "Platform", type: "select", options: Object.entries(platforms), half: true },
    { key: "host", label: "Hostname or IP", half: true },
    { key: "port", label: "SSH port", type: "number", half: true },
    { key: "username", label: "Username", half: true, help: "Use a read-only account unless agents should be able to push approved changes." },
    { key: "password", label: device && device.has_password ? "Password (stored - blank keeps it)" : "Password", type: "secret", half: true },
    { key: "enable_password", label: device && device.has_enable ? "Enable secret (stored - blank keeps it)" : "Enable secret (optional)", type: "secret", half: true },
    { key: "transport", label: "Connection", type: "select", options: [["ssh", "SSH"], ["simulated", "Simulated (canned output, for testing)"]], half: true },
    { key: "site", label: "Site", half: true },
    { key: "tags", label: "Tags (comma separated)", half: true },
    { key: "notes", label: "Notes", type: "textarea", rows: 2 },
    { key: "allow_config", label: "Allow configuration changes", type: "bool", help: "approved change requests may be pushed to this device" },
  ], d);
  modal({ title: device ? `Edit ${device.name}` : "Add device", body: form.el, actions: [{ label: "Save device", run: async () => {
    const v = form.values();
    const body = { ...v, port: v.port || 22, password: v.password === "" ? null : v.password, enable_password: v.enable_password === "" ? null : v.enable_password };
    const ok = await attempt(() => (device ? api(`/devices/${device.id}`, { method: "PUT", body }) : api("/devices", { method: "POST", body })), "Device saved");
    if (!ok) return false;
    ctx.load();
  } }] });
}

// ================================================================ integrations
views.integrations = async (root) => {
  const [channels, keys] = await Promise.all([api("/channels"), api("/keys")]);
  const origin = state.me.public_url || location.origin;
  const cards = channels.map((ch) => {
    const form = buildForm(ch.schema, ch.values);
    const st = ch.status;
    return h("div", { class: "card" },
      h("div", { class: "agent-head", style: "margin-bottom:12px" }, h("div", { class: "agent-name" }, ch.title),
        st.enabled ? pill("enabled", "ok") : pill("off", "")),
      st.error ? h("div", { class: "callout bad" }, "Last error: " + st.error) : null,
      st.endpoints ? h("div", { class: "callout info" }, "Endpoints to give Teams - bot messaging endpoint: ", h("code", {}, st.endpoints.bot),
        " · outgoing webhook: ", h("code", {}, st.endpoints.outgoing_webhook)) : null,
      ch.id === "email" && st.last_poll ? h("div", { class: "muted small", style: "margin-bottom:8px" },
        `Last mailbox check: ${st.last_poll} · ignored (sender not allowed): ${st.ignored}`) : null,
      form.el,
      h("div", { class: "actions", style: "margin-top:14px" },
        h("button", { class: "btn primary", onclick: async () => {
          if (await attempt(() => api(`/channels/${ch.id}`, { method: "PUT", body: form.values() }), `${ch.title} saved`)) route();
        } }, "Save"),
        h("button", { class: "btn", onclick: async (ev) => {
          ev.target.disabled = true;
          const r = await attempt(() => api(`/channels/${ch.id}/test`, { method: "POST" }));
          if (r) toast(`${ch.title}: ${r.message}`, !r.ok);
          ev.target.disabled = false;
        } }, ch.id === "notifications" ? "Send test notification" : "Test saved settings")));
  });
  const keyName = h("input", { type: "text", placeholder: "Key name, e.g. ServiceNow", style: "max-width:240px" });
  const keyScope = h("select", { style: "max-width:240px" }, h("option", { value: "tasks" }, "Tasks only (submit and read tasks)"),
    h("option", { value: "admin" }, "Full administration"));
  const example = `curl -X POST ${origin}/api/v1/tasks \\\n  -H "Authorization: Bearer <API key>" -H "Content-Type: application/json" \\\n`
    + `  -d '{"title": "BGP neighbor down on leaf-dc1-01", "description": "...", "agent": "Arista Expert", "callback_url": "https://example.com/hook"}'\n\n`
    + `# Ask and wait for the answer\ncurl -X POST ${origin}/api/v1/ask -H "Authorization: Bearer <API key>" \\\n`
    + `  -H "Content-Type: application/json" -d '{"question": "What is the wildcard mask for a /22?"}'`;
  fill(root, 
    pageHead("Integrations", "Connect the channels people and systems use to reach the agents, and where results are reported."),
    h("div", { class: "grid" }, cards,
      h("div", { class: "card" },
        h("div", { class: "agent-head", style: "margin-bottom:12px" }, h("div", { class: "agent-name" }, "REST API"), pill("always on", "ok")),
        h("p", { class: "muted", style: "margin-top:0" }, "Any system can create tasks and read results. Full reference: ",
          h("a", { href: "/api/docs", target: "_blank" }, "/api/docs")),
        h("pre", {}, example),
        h("h3", { style: "margin-top:14px" }, "API keys"),
        table(["Name", "Key", "Scope", "Created", "Last used", ""], keys.map((k) => h("tr", {},
          h("td", {}, k.name), h("td", { class: "mono" }, k.prefix + "..."), h("td", {}, k.scope), h("td", {}, ago(k.created_at)),
          h("td", {}, k.last_used_at ? ago(k.last_used_at) : "never"),
          h("td", {}, h("button", { class: "btn sm danger", onclick: async () => {
            if (!confirm(`Revoke API key "${k.name}"?`)) return;
            if (await attempt(() => api(`/keys/${k.id}`, { method: "DELETE" }), "Key revoked")) route();
          } }, "Revoke")))), "No API keys yet."),
        h("div", { class: "actions", style: "margin-top:12px" }, keyName, keyScope,
          h("button", { class: "btn", onclick: async () => {
            const created = await attempt(() => api("/keys", { method: "POST", body: { name: keyName.value || "unnamed", scope: keyScope.value } }));
            if (!created) return;
            modal({ title: "API key created", body: h("div", {}, h("p", {}, "Copy this key now. It is not shown again."),
              h("div", { class: "keybox" }, created.key)) });
            route();
          } }, "Create API key")))));
};

// ===================================================================== reports
let reportDays = 7;
views.reports = async (root, args, ctx) => {
  const body = h("div", {});
  const range = h("select", { "aria-label": "Period", style: "width:auto" }, [[1, "Last 24 hours"], [7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]]
    .map(([v, t]) => h("option", { value: v, selected: v === reportDays }, t)));
  range.addEventListener("change", () => { reportDays = Number(range.value); ctx.load(); });
  fill(root, 
    pageHead("Reports", "Completion tracking across agents, models and channels.", range,
      h("a", { class: "btn", href: "#", onclick: (ev) => { ev.preventDefault(); window.open(`/api/v1/reports/tasks.csv?days=${reportDays}`); } }, "Export CSV"),
      h("button", { class: "btn", onclick: async () => {
        const text = await attempt(() => api(`/reports/digest?days=${reportDays}`));
        if (text) modal({ title: "Summary report", wide: true, body: h("div", { class: "md", html: md(text) }) });
      } }, "View summary"),
      h("button", { class: "btn primary", onclick: async () => {
        const r = await attempt(() => api(`/reports/send?days=${reportDays}`, { method: "POST" }));
        if (r) toast(r.message, !r.ok);
      } }, "Send report now")),
    body);
  ctx.load = async () => {
    const s = await api(`/reports/summary?days=${reportDays}`);
    const t = s.totals;
    const pct = (v) => (v === null ? "-" : `${v}%`);
    const peak = Math.max(1, ...s.by_day.map((d) => d.created));
    const breakdown = (title, rows, columns) => h("div", {}, h("h3", {}, title), table(columns.map((c) => c[0]),
      rows.map((r) => h("tr", {}, columns.map((c) => h("td", { class: c[2] || "" }, c[1](r))))), "No data in this period."));
    const share = (r) => h("div", { class: "hbar", title: `${r.total}` }, h("i", { style: `width:${Math.round(100 * r.total / Math.max(1, t.tasks))}%` }));
    fill(body, 
      h("div", { class: "kpis" },
        kpi("Requests", t.top_level, `${t.sub_tasks} agent sub-tasks`),
        kpi("Completed", t.completed, `success rate ${pct(t.success_rate)}`),
        kpi("Failed", t.failed, `${t.open} still open`, t.failed ? "alert" : ""),
        kpi("Avg. time to complete", fmtDur(t.avg_seconds)),
        kpi("Escalated", pct(t.escalation_rate), `${t.escalated} task(s) climbed the ladder`),
        kpi("Answered locally", pct(t.local_share), "share of completed tasks"),
        kpi("Needs review", t.needs_review, "low-confidence answers", t.needs_review ? "warn" : ""),
        kpi("Est. model cost", `$${t.cost.toFixed(2)}`, `${((t.input_tokens + t.output_tokens) / 1000).toFixed(1)}k tokens`)),
      h("div", { class: "card" }, h("h3", {}, "Tasks per day (red = failed)"),
        s.by_day.length ? h("div", { class: "bars" }, s.by_day.map((d) => h("div", { class: "bar-col", title: `${d.day}: ${d.created} created, ${d.completed} completed, ${d.failed} failed` },
          h("div", { class: "bar", style: `height:${Math.round(100 * d.created / peak)}%` }, d.failed ? h("div", { class: "f", style: `height:${Math.round(100 * d.failed / d.created)}%` }) : null),
          h("div", { class: "lbl" }, d.day.slice(5))))) : h("div", { class: "empty" }, "No tasks in this period.")),
      h("div", { class: "section grid cols-2" },
        breakdown("By agent", s.by_agent, [["Agent", (r) => r.name], ["Tasks", (r) => r.total, "num"], ["Completed", (r) => r.completed, "num"],
          ["Failed", (r) => r.failed, "num"], ["Open", (r) => r.open, "num"], ["Avg. time", (r) => fmtDur(r.avg_seconds), "num"], ["Share", share]]),
        breakdown("Completed by model tier", s.by_model, [["Model", (r) => r.name], ["Completed", (r) => r.completed, "num"],
          ["After escalation", (r) => r.escalated, "num"], ["Avg. time", (r) => fmtDur(r.avg_seconds), "num"], ["Est. cost", (r) => `$${r.cost.toFixed(2)}`, "num"]])),
      h("div", { class: "section grid cols-2" },
        breakdown("By channel", s.by_source, [["Channel", (r) => r.name], ["Requests", (r) => r.total, "num"], ["Completed", (r) => r.completed, "num"],
          ["Failed", (r) => r.failed, "num"]]),
        h("div", {}, h("h3", {}, "Recent requests"), table(["#", "Task", "Status", "Agent", "Time"],
          s.recent.slice(0, 12).map((r) => h("tr", { class: "click", onclick: () => { location.hash = `#/tasks/${r.id}`; } },
            h("td", { class: "muted" }, r.id), h("td", {}, h("div", { class: "title-cell", style: "max-width:260px" }, r.title)),
            h("td", {}, statusPill(r)), h("td", {}, r.agent || "-"), h("td", { class: "num nowrap" }, fmtDur(r.seconds)))), "No requests in this period."))));
  };
  await ctx.load();
};
