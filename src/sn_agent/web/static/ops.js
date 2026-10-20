// The ops console: what the ticket agent did on each ticket, what the chatbot
// handled, and how both are doing over time.

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const charts = {};

const OUTCOME_LABEL = {
  RESOLUTION_FOUND: ["Fix suggested", "good"],
  INSUFFICIENT_EVIDENCE: ["Evidence too weak", "warn"],
  SIMILAR_NO_RESOLUTION_RECORDED: ["Seen before, no fix recorded", "warn"],
  NO_HISTORY: ["No similar ticket", ""],
};
const CONV_LABEL = {
  solved: ["Solved, no ticket", "good"],
  ticket_raised: ["Ticket raised", "warn"],
  ticket_drafted: ["Ticket drafted (dry run)", "warn"],
  abandoned: ["Ended without result", ""],
  open: ["In progress", ""],
};

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function when(seconds) {
  return seconds ? new Date(seconds * 1000).toLocaleString() : "-";
}
function pill([label, tone]) {
  return `<span class="pill ${tone}">${esc(label)}</span>`;
}
async function getJSON(url) {
  const response = await fetch(url);
  if (response.status === 401) location.href = "/login";
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json();
}

// --- tabs ---------------------------------------------------------------------

document.querySelectorAll(".tabs button").forEach((button) =>
  button.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === button));
    for (const name of ["dashboard", "tickets", "conversations"]) $(`tab-${name}`).hidden = name !== button.dataset.tab;
    ({ dashboard: loadDashboard, tickets: loadRuns, conversations: loadConversations })[button.dataset.tab]();
  })
);

// --- dashboard ----------------------------------------------------------------

function kpi(label, value, sub) {
  return `<div class="card kpi"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="sub">${esc(sub || "")}</div></div>`;
}

function drawBar(id, labels, values, { horizontal = false } = {}) {
  charts[id]?.destroy();
  const ink = css("--muted");
  const grid = css("--line");
  charts[id] = new Chart($(id), {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: css("--accent"), borderRadius: 4, maxBarThickness: 36 }] },
    options: {
      indexAxis: horizontal ? "y" : "x",
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: ink }, grid: { color: horizontal ? grid : "transparent" }, beginAtZero: true },
        y: { ticks: { color: ink, precision: 0 }, grid: { color: horizontal ? "transparent" : grid }, beginAtZero: true },
      },
    },
  });
}

async function loadDashboard() {
  const d = await getJSON(`/api/ops/dashboard?days=${$("days").value}`);
  const pct = d.deflection_rate == null ? "-" : `${Math.round(d.deflection_rate * 100)}%`;
  const seconds = (v) => (v == null ? "-" : `${v.toFixed(1)} s`);
  $("kpis").innerHTML = [
    kpi("Solved without a ticket", pct, `${d.solved_without_ticket} of ${d.closed_conversations} finished chats`),
    kpi("Tickets raised by the chatbot", d.tickets_raised, Object.entries(d.raised_by_priority).map(([p, n]) => `P${p}: ${n}`).join("  ") || "none"),
    kpi("Tickets answered by the agent", d.runs, `over the last ${d.days} days`),
    kpi("Chatbot reply time", seconds(d.turn_first_token_s.p50), `median to first word; p95 ${seconds(d.turn_first_token_s.p95)}`),
    kpi("Agent run time", seconds(d.run_latency_s.p50), `median per ticket; p95 ${seconds(d.run_latency_s.p95)}`),
  ].join("");
  drawBar("chartRuns", Object.keys(d.runs_per_day), Object.values(d.runs_per_day));
  drawBar("chartConfidence", Object.keys(d.confidence_histogram), Object.values(d.confidence_histogram));
  drawBar(
    "chartOutcomes",
    Object.keys(d.run_outcomes).map((k) => (OUTCOME_LABEL[k] || [k])[0]),
    Object.values(d.run_outcomes),
    { horizontal: true }
  );
  drawBar("chartGroups", Object.keys(d.raised_by_group), Object.values(d.raised_by_group), { horizontal: true });
}
$("days").addEventListener("change", loadDashboard);

// --- tickets answered -----------------------------------------------------------

let runs = [];

function renderRuns() {
  const text = $("ticketFilter").value.trim().toUpperCase();
  const outcome = $("outcomeFilter").value;
  const rows = runs.filter((r) => (!text || r.ticket_number.includes(text)) && (!outcome || r.outcome === outcome));
  const body = $("runsTable").querySelector("tbody");
  body.innerHTML = rows.length
    ? rows
        .map(
          (r) => `<tr data-id="${esc(r.run_id)}">
        <td><strong>${esc(r.ticket_number)}</strong></td>
        <td>${esc(when(r.started_at))}</td>
        <td>${pill(OUTCOME_LABEL[r.outcome] || [r.outcome, ""])}</td>
        <td>${r.confidence == null ? "-" : `${Math.round(r.confidence)} <span class="muted small">${esc(r.confidence_level)}</span>`}</td>
        <td>${r.latency_s == null ? "-" : `${r.latency_s.toFixed(0)} s`}</td>
        <td>${r.delivered ? "Posted" : '<span class="muted">Dry run</span>'}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="6" class="empty">No runs recorded yet. They appear here as the agent answers tickets.</td></tr>`;
  body.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openRun(tr.dataset.id)));
}

async function loadRuns() {
  runs = await getJSON("/api/ops/runs");
  const select = $("outcomeFilter");
  const current = select.value;
  select.innerHTML = '<option value="">All outcomes</option>' +
    [...new Set(runs.map((r) => r.outcome))].map((o) => `<option value="${esc(o)}">${esc((OUTCOME_LABEL[o] || [o])[0])}</option>`).join("");
  select.value = current;
  renderRuns();
}
$("ticketFilter").addEventListener("input", renderRuns);
$("outcomeFilter").addEventListener("change", renderRuns);

function openDrawer(html) {
  $("drawerBody").innerHTML = html;
  $("drawer").classList.add("open");
  $("drawerBg").classList.add("open");
}
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawerBg").classList.remove("open");
}
$("closeDrawer").addEventListener("click", closeDrawer);
$("drawerBg").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

async function openRun(runId) {
  const run = await getJSON(`/api/ops/runs/${encodeURIComponent(runId)}`);
  const r = run.result || {};
  const conf = r.confidence || {};
  const link = r.ticket_sys_id && run.instance_url
    ? `<a target="_blank" rel="noopener" href="${esc(run.instance_url)}/nav_to.do?uri=${esc(r.table || "incident")}.do%3Fsys_id%3D${esc(r.ticket_sys_id)}">Open in ServiceNow</a>`
    : "";
  const signals = Object.entries(conf.signals || {})
    .map(([name, value]) => `<div class="signal"><span>${esc(name.replace(/_/g, " "))}</span><div class="bar"><span style="width:${Math.round(value * 100)}%"></span></div><span>${value.toFixed(2)}</span></div>`)
    .join("");
  const steps = (run.steps || [])
    .map((s) => {
      const major = /^step \d/.test(s.message) ? "major" : "";
      const t = new Date(s.at * 1000).toLocaleTimeString();
      return `<li class="${major} ${esc(s.level)}"><span class="t">${esc(t)}</span>${esc(s.message)}</li>`;
    })
    .join("");
  const evidence = (r.evidence || [])
    .map((e) => `<li><strong>${esc(e.number)}</strong> <span class="muted">${esc(e.field)}</span><div class="small">${esc(e.snippet)}</div></li>`)
    .join("");
  const related = (r.active_related || []).map((a) => `${esc(a.number)} (${esc(a.relation.replace("_", " "))}, ${esc(a.assignment_group || "no group")})`).join(", ");
  openDrawer(`
    <h2>${esc(r.ticket_number || run.ticket_number)}</h2>
    <div class="meta">${pill(OUTCOME_LABEL[r.outcome] || [r.outcome, ""])}
      <span class="pill">confidence ${conf.score == null ? "-" : Math.round(conf.score)} (${esc(conf.level || "-")})</span>
      <span class="pill">${(r.latency_s || 0).toFixed(0)} s, ${esc(r.iterations || 0)} research pass(es)</span>
      <span class="pill">${run.delivered ? "Note posted" : "Dry run"}</span> ${link}</div>
    ${r.major_incident_warning ? '<p class="pill bad">Possible major incident</p>' : ""}
    ${related ? `<p class="small">Open tickets for the same issue: ${related}</p>` : ""}
    <h3>Confidence breakdown</h3>${signals || '<div class="muted small">No score: no documented fix was found.</div>'}
    <h3>Steps the agent took</h3>${steps ? `<ul class="steps">${steps}</ul>` : '<div class="muted small">No step log was recorded for this run.</div>'}
    <h3>Evidence cited</h3>${evidence ? `<ul class="small">${evidence}</ul>` : '<div class="muted small">None.</div>'}
    <h3>Work note</h3><pre class="note">${esc(r.note || "")}</pre>`);
}

// --- conversations ------------------------------------------------------------

async function loadConversations() {
  const rows = await getJSON("/api/ops/conversations");
  const body = $("convTable").querySelector("tbody");
  body.innerHTML = rows.length
    ? rows
        .map(
          (c) => `<tr data-id="${esc(c.id)}">
        <td>${esc(when(c.started_at))}</td><td>${esc(c.user_email || "-")}</td>
        <td>${c.channel === "voice" ? "Voice" : "Text"}</td><td>${esc(c.summary || "-")}</td>
        <td>${pill(CONV_LABEL[c.outcome] || [c.outcome, ""])}</td>
        <td>${esc(c.incident_number || (c.priority ? `P${c.priority} ${c.assignment_group}` : "-"))}</td></tr>`
        )
        .join("")
    : `<tr><td colspan="6" class="empty">No chatbot conversations yet.</td></tr>`;
  body.querySelectorAll("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => openConversation(tr.dataset.id)));
}

async function openConversation(id) {
  const c = await getJSON(`/api/ops/conversations/${encodeURIComponent(id)}`);
  const transcript = (c.transcript || [])
    .map((t) => `<div class="bubble ${t.role === "user" ? "user" : "agent"}">${esc(t.text)}</div>`)
    .join("");
  const tools = (c.events || [])
    .filter((e) => e.type === "tool")
    .map((e) => {
      const result = e.result || {};
      const summary = result.status || result.read_back || result.error || "";
      return `<li><span class="t">${esc(new Date(e.at * 1000).toLocaleTimeString())}</span><strong>${esc(e.name)}</strong> ${esc(summary)} <span class="muted">(${e.seconds}s)</span></li>`;
    })
    .join("");
  const turns = (c.events || []).filter((e) => e.type === "turn" && e.first_token_s != null);
  const avg = turns.length ? (turns.reduce((s, e) => s + e.first_token_s, 0) / turns.length).toFixed(1) : null;
  openDrawer(`
    <h2>${esc(c.summary || "Conversation")}</h2>
    <div class="meta">${pill(CONV_LABEL[c.outcome] || [c.outcome, ""])}
      <span class="pill">${c.channel === "voice" ? "Voice" : "Text"}</span>
      <span class="pill">${esc(c.user_email || "unknown user")}</span>
      ${c.incident_number ? `<span class="pill">${esc(c.incident_number)}</span>` : ""}
      ${c.priority ? `<span class="pill">P${esc(c.priority)} to ${esc(c.assignment_group)}</span>` : ""}
      ${avg ? `<span class="pill">avg ${avg} s to first word</span>` : ""}</div>
    <h3>What the assistant did</h3>${tools ? `<ul class="steps">${tools}</ul>` : '<div class="muted small">No searches or tickets.</div>'}
    <h3>Transcript</h3><div class="messages" style="padding:0">${transcript}</div>`);
}

(async function init() {
  const me = await getJSON("/api/me");
  $("who").textContent = me.name || me.email;
  loadDashboard();
})();
