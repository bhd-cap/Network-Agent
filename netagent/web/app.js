/* BlackHawk NetAgent console - core: helpers, API client, router, live updates. No build step. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { me: null, view: null, modalOpen: 0, pendingChanges: 0 };
const views = {};

// ---------------------------------------------------------------- DOM helpers
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "html") el.innerHTML = value; // only ever fed output of md()/esc()
    else if (key === "style") el.setAttribute("style", value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el && key !== "list") el[key] = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

/* Replace an element's children, ignoring null/false entries and flattening arrays. */
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* Small, safe Markdown renderer: input is escaped first, then a limited set of constructs is turned into tags. */
function md(source) {
  const lines = String(source ?? "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  const inline = (text) => esc(text)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\s][^*]*)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  let i = 0;
  let para = [];
  const flush = () => { if (para.length) { out.push("<p>" + para.map(inline).join("<br>") + "</p>"); para = []; } };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flush();
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;
      out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>");
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) { flush(); out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flush(); out.push("<hr>"); i++; continue; }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      let table = "<table><thead><tr>" + cells(line).map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        table += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
        i++;
      }
      out.push(table + "</tbody></table>");
      continue;
    }
    const bullet = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (bullet) {
      flush();
      const ordered = /\d/.test(bullet[1]);
      const items = [];
      while (i < lines.length) {
        const m = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (!m) break;
        items.push("<li>" + inline(m[2]) + "</li>");
        i++;
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    para.push(line);
    i++;
  }
  flush();
  return out.join("");
}

// ------------------------------------------------------------------ formatting
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })
    : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}
function ago(iso) {
  if (!iso) return "-";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
function fmtDur(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}
const PRIORITY = { 1: "urgent", 2: "high", 3: "normal", 4: "low" };
function pill(text, cls) { return h("span", { class: `pill ${cls || String(text).toLowerCase().replace(/\s+/g, "-")}` }, text); }
function statusPill(task) {
  if (task.status === "completed" && task.needs_review) return pill("needs review", "review");
  return pill(task.status);
}

// ---------------------------------------------------------------------- API
async function api(path, options = {}) {
  const init = { method: options.method || "GET", credentials: "same-origin", headers: { "X-Requested-With": "netagent" } };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const resp = await fetch("/api/v1" + path, init);
  if (resp.status === 401 && path !== "/auth/login") { showLogin(); throw new Error("Signed out"); }
  const type = resp.headers.get("content-type") || "";
  const data = type.includes("json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : `Request failed (${resp.status})`);
  }
  return data;
}
/* Run an action, show errors as a toast. Returns undefined on failure. */
async function attempt(fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toast(okMessage);
    return result === undefined ? true : result;
  } catch (err) {
    toast(err.message || String(err), true);
    return undefined;
  }
}

// ------------------------------------------------------------ toast and modal
function toast(message, bad) {
  const el = h("div", { class: "toast" + (bad ? " bad" : "") }, message);
  $("#toasts").append(el);
  setTimeout(() => el.remove(), bad ? 6500 : 3200);
}

function modal({ title, body, actions = [], wide = false }) {
  state.modalOpen++;
  const close = () => { if (back.isConnected) { back.remove(); state.modalOpen--; } };
  const footer = h("footer", {}, h("button", { class: "btn", type: "button", onclick: close }, "Close"));
  for (const action of actions) {
    footer.append(h("button", {
      class: "btn " + (action.class || "primary"), type: "button",
      onclick: async (ev) => {
        ev.target.disabled = true;
        try { if (await action.run() !== false) close(); } finally { ev.target.disabled = false; }
      },
    }, action.label));
  }
  const back = h("div", { class: "modal-back", onmousedown: (ev) => { if (ev.target === back) close(); } },
    h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
      h("header", {}, h("h2", { style: "margin:0" }, title), h("button", { class: "x", type: "button", "aria-label": "Close", onclick: close }, "×")),
      h("div", { class: "body" }, body), footer));
  back.addEventListener("keydown", (ev) => { if (ev.key === "Escape") close(); });
  $("#modal-root").append(back);
  const first = back.querySelector("input:not([type=radio]):not([type=checkbox]), textarea, select");
  if (first) first.focus();
  return close;
}

/* Build a form from field specs; returns {el, values()}. */
function buildForm(fields, values = {}) {
  const controls = {};
  const el = h("div", { class: "form" });
  let row = null;
  for (const f of fields) {
    let input;
    const value = values[f.key] ?? f.default ?? "";
    if (f.type === "bool") {
      input = h("input", { type: "checkbox", checked: !!value });
      const label = h("label", { class: "check" }, input, h("span", {}, f.label, f.help ? h("span", { class: "help" }, " - " + f.help) : null));
      controls[f.key] = () => input.checked;
      el.append(label);
      row = null;
      continue;
    }
    if (f.type === "select") {
      input = h("select", {}, (f.options || []).map((o) => {
        const [val, text] = Array.isArray(o) ? o : [o, o];
        return h("option", { value: val, selected: String(val) === String(value) }, text);
      }));
    } else if (f.type === "textarea") {
      input = h("textarea", { rows: f.rows || 4, placeholder: f.placeholder || "" });
      input.value = value;
    } else {
      input = h("input", {
        type: f.type === "secret" ? "password" : f.type === "number" ? "number" : "text", value,
        placeholder: f.placeholder || "", autocomplete: f.type === "secret" ? "new-password" : "off", step: f.step || null,
      });
    }
    controls[f.key] = () => (f.type === "number" ? (input.value === "" ? null : Number(input.value)) : input.value);
    const label = h("label", {}, f.label, input, f.help ? h("span", { class: "help" }, f.help) : null);
    if (f.half) {
      if (!row || row.children.length >= 2) { row = h("div", { class: "row" }); el.append(row); }
      row.append(label);
    } else { el.append(label); row = null; }
  }
  return { el, values: () => Object.fromEntries(Object.entries(controls).map(([k, get]) => [k, get()])) };
}

// ---------------------------------------------------------------- shell/router
const NAV = [
  ["dashboard", "Dashboard"], ["tasks", "Tasks"], ["chat", "Chat"], ["agents", "Agents"], ["approvals", "Approvals"], null,
  ["profiles", "Profiles"], ["models", "Models & Ladders"], ["devices", "Devices"], ["integrations", "Integrations"], ["reports", "Reports"],
];

function renderNav(active) {
  const nav = $("#nav");
  nav.replaceChildren(...NAV.map((item) => {
    if (!item) return h("div", { class: "sep" });
    const [id, label] = item;
    return h("a", { href: `#/${id}`, class: id === active ? "active" : "" }, label,
      id === "approvals" && state.pendingChanges ? h("span", { class: "count" }, state.pendingChanges) : null);
  }));
}

async function route() {
  if (!state.me) return;
  const [name, ...args] = (location.hash.replace(/^#\/?/, "") || "dashboard").split("/");
  const view = views[name] || views.dashboard;
  state.view = { name: views[name] ? name : "dashboard", args, load: null };
  renderNav(state.view.name);
  const root = $("#view");
  root.replaceChildren(h("div", { class: "empty" }, "Loading..."));
  try {
    await view(root, args, state.view);
  } catch (err) {
    if (err.message !== "Signed out") root.replaceChildren(h("div", { class: "callout bad" }, err.message || String(err)));
  }
}

let refreshTimer = null;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    refreshBadge();
    if (state.modalOpen || !state.view || !state.view.load) return;
    try { await state.view.load(); } catch (err) { /* next event retries */ }
  }, 350);
}

async function refreshBadge() {
  try {
    const o = await api("/overview");
    if (o.counts.pending_changes !== state.pendingChanges) {
      state.pendingChanges = o.counts.pending_changes;
      renderNav(state.view && state.view.name);
    }
  } catch (err) { /* ignore */ }
}

let source = null;
function connectEvents() {
  if (source) source.close();
  source = new EventSource("/api/v1/events");
  const conn = $("#conn");
  source.onopen = () => { conn.classList.add("live"); $("#conn-text").textContent = "live"; };
  source.onerror = () => { conn.classList.remove("live"); $("#conn-text").textContent = "reconnecting"; };
  source.onmessage = scheduleRefresh;
}

function showLogin() {
  state.me = null;
  if (source) { source.close(); source = null; }
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-token").focus();
}

async function start() {
  try {
    state.me = await api("/auth/me");
  } catch (err) { return; }
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#demo-banner").hidden = !state.me.demo;
  $("#version").textContent = `v${state.me.version}`;
  connectEvents();
  await refreshBadge();
  route();
}

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
}

document.addEventListener("DOMContentLoaded", () => {
  let theme = null;
  try { theme = localStorage.getItem("netagent.theme"); } catch (err) { /* storage unavailable */ }
  applyTheme(theme);
  $("#theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("netagent.theme", next); } catch (err) { /* ignore */ }
  });
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const error = $("#login-error");
    error.hidden = true;
    try {
      await api("/auth/login", { method: "POST", body: { token: $("#login-token").value } });
      $("#login-token").value = "";
      start();
    } catch (err) { error.textContent = err.message; error.hidden = false; }
  });
  $("#logout").addEventListener("click", async () => { await attempt(() => api("/auth/logout", { method: "POST" })); showLogin(); });
  window.addEventListener("hashchange", route);
  start();
});
