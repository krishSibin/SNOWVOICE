// The ops console: what the ticket agent did on each ticket, what the chatbot
// handled, and how both are doing over time.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import { getJSON, when, type Me } from "../shared/api";
import { Pill, TopBar, type Tone } from "../shared/ui";
import { BarChart } from "./BarChart";

const OUTCOME_LABEL: Record<string, [string, Tone]> = {
  RESOLUTION_FOUND: ["Fix suggested", "good"],
  INSUFFICIENT_EVIDENCE: ["Evidence too weak", "warn"],
  SIMILAR_NO_RESOLUTION_RECORDED: ["Seen before, no fix recorded", "warn"],
  NO_HISTORY: ["No similar ticket", ""],
};
const CONV_LABEL: Record<string, [string, Tone]> = {
  solved: ["Solved, no ticket", "good"],
  ticket_raised: ["Ticket raised", "warn"],
  ticket_drafted: ["Ticket drafted (dry run)", "warn"],
  abandoned: ["Ended without result", ""],
  open: ["In progress", ""],
};

function Outcome({ value, labels }: { value: string; labels: Record<string, [string, Tone]> }) {
  const [label, tone] = labels[value] || [value, ""];
  return <Pill tone={tone}>{label}</Pill>;
}

// --- API shapes ---------------------------------------------------------------

interface Percentiles {
  p50: number | null;
  p95: number | null;
}
interface Dashboard {
  days: number;
  deflection_rate: number | null;
  solved_without_ticket: number;
  closed_conversations: number;
  tickets_raised: number;
  raised_by_priority: Record<string, number>;
  raised_by_group: Record<string, number>;
  runs: number;
  runs_per_day: Record<string, number>;
  confidence_histogram: Record<string, number>;
  run_outcomes: Record<string, number>;
  turn_first_token_s: Percentiles;
  run_latency_s: Percentiles;
}
interface RunRow {
  run_id: string;
  ticket_number: string;
  started_at: number;
  outcome: string;
  confidence: number | null;
  confidence_level?: string;
  latency_s: number | null;
  delivered: boolean;
}
interface RunDetail {
  ticket_number: string;
  delivered: boolean;
  instance_url?: string;
  steps?: { at: number; level: string; message: string }[];
  result?: {
    ticket_number?: string;
    ticket_sys_id?: string;
    table?: string;
    outcome: string;
    latency_s?: number;
    iterations?: number;
    note?: string;
    major_incident_warning?: boolean;
    confidence?: { score?: number; level?: string; signals?: Record<string, number> };
    evidence?: { number: string; field: string; snippet: string }[];
    active_related?: { number: string; relation: string; assignment_group?: string }[];
  };
}
interface ConvRow {
  id: string;
  started_at: number;
  user_email?: string;
  channel: string;
  summary?: string;
  outcome: string;
  incident_number?: string;
  priority?: number;
  assignment_group?: string;
}
interface ConvDetail extends ConvRow {
  transcript?: { role: string; text: string }[];
  events?: {
    type: string;
    at: number;
    name?: string;
    seconds?: number;
    first_token_s?: number;
    result?: { status?: string; read_back?: string; error?: string };
  }[];
}

// --- dashboard ----------------------------------------------------------------

function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: string }) {
  return (
    <div className="card kpi">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

function ChartCard({ title, data, horizontal, relabel }: {
  title: string;
  data: Record<string, number>;
  horizontal?: boolean;
  relabel?: (key: string) => string;
}) {
  const labels = useMemo(() => Object.keys(data).map((k) => (relabel ? relabel(k) : k)), [data, relabel]);
  const values = useMemo(() => Object.values(data), [data]);
  return (
    <div className="card chart-card">
      <h3>{title}</h3>
      <BarChart labels={labels} values={values} horizontal={horizontal} />
    </div>
  );
}

const outcomeName = (k: string) => (OUTCOME_LABEL[k] || [k])[0];
const seconds = (v: number | null) => (v == null ? "-" : `${v.toFixed(1)} s`);

function DashboardTab() {
  const [days, setDays] = useState(30);
  const [d, setD] = useState<Dashboard | null>(null);

  useEffect(() => {
    getJSON<Dashboard>(`/api/ops/dashboard?days=${days}`).then(setD, console.warn);
  }, [days]);

  return (
    <section>
      <div className="toolbar">
        <label className="small muted" htmlFor="days">Period</label>
        <select id="days" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option>
          <option value={90}>Last 90 days</option>
        </select>
      </div>
      {d && (
        <>
          <div className="kpis">
            <Kpi
              label="Solved without a ticket"
              value={d.deflection_rate == null ? "-" : `${Math.round(d.deflection_rate * 100)}%`}
              sub={`${d.solved_without_ticket} of ${d.closed_conversations} finished chats`}
            />
            <Kpi
              label="Tickets raised by the chatbot"
              value={d.tickets_raised}
              sub={Object.entries(d.raised_by_priority).map(([p, n]) => `P${p}: ${n}`).join("  ") || "none"}
            />
            <Kpi label="Tickets answered by the agent" value={d.runs} sub={`over the last ${d.days} days`} />
            <Kpi
              label="Chatbot reply time"
              value={seconds(d.turn_first_token_s.p50)}
              sub={`median to first word; p95 ${seconds(d.turn_first_token_s.p95)}`}
            />
            <Kpi
              label="Agent run time"
              value={seconds(d.run_latency_s.p50)}
              sub={`median per ticket; p95 ${seconds(d.run_latency_s.p95)}`}
            />
          </div>
          <div className="charts">
            <ChartCard title="Tickets answered by the agent, per day" data={d.runs_per_day} />
            <ChartCard title="Agent confidence (0-100)" data={d.confidence_histogram} />
            <ChartCard title="Agent outcomes" data={d.run_outcomes} horizontal relabel={outcomeName} />
            <ChartCard title="Chatbot tickets by team" data={d.raised_by_group} horizontal />
          </div>
        </>
      )}
    </section>
  );
}

// --- drawers ------------------------------------------------------------------

function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="drawer-bg open" onClick={onClose} />
      <aside className="drawer open" aria-modal="true" role="dialog">
        <button onClick={onClose} style={{ float: "right" }}>Close</button>
        {children}
      </aside>
    </>
  );
}

function RunView({ run }: { run: RunDetail }) {
  const r = run.result || { outcome: "" };
  const conf = r.confidence || {};
  const link =
    r.ticket_sys_id && run.instance_url
      ? `${run.instance_url}/nav_to.do?uri=${encodeURIComponent(`${r.table || "incident"}.do?sys_id=${r.ticket_sys_id}`)}`
      : null;
  const related = (r.active_related || [])
    .map((a) => `${a.number} (${a.relation.replace("_", " ")}, ${a.assignment_group || "no group"})`)
    .join(", ");
  const signals = Object.entries(conf.signals || {});
  return (
    <>
      <h2>{r.ticket_number || run.ticket_number}</h2>
      <div className="meta">
        <Outcome value={r.outcome} labels={OUTCOME_LABEL} />
        <Pill>confidence {conf.score == null ? "-" : Math.round(conf.score)} ({conf.level || "-"})</Pill>
        <Pill>{(r.latency_s || 0).toFixed(0)} s, {r.iterations || 0} research pass(es)</Pill>
        <Pill>{run.delivered ? "Note posted" : "Dry run"}</Pill>
        {link && <a target="_blank" rel="noopener" href={link}>Open in ServiceNow</a>}
      </div>
      {r.major_incident_warning && <p className="pill bad">Possible major incident</p>}
      {related && <p className="small">Open tickets for the same issue: {related}</p>}

      <h3>Confidence breakdown</h3>
      {signals.length ? (
        signals.map(([name, value]) => (
          <div className="signal" key={name}>
            <span>{name.replace(/_/g, " ")}</span>
            <div className="bar"><span style={{ width: `${Math.round(value * 100)}%` }} /></div>
            <span>{value.toFixed(2)}</span>
          </div>
        ))
      ) : (
        <div className="muted small">No score: no documented fix was found.</div>
      )}

      <h3>Steps the agent took</h3>
      {run.steps?.length ? (
        <ul className="steps">
          {run.steps.map((s, i) => (
            <li key={i} className={`${/^step \d/.test(s.message) ? "major" : ""} ${s.level}`}>
              <span className="t">{new Date(s.at * 1000).toLocaleTimeString()}</span>
              {s.message}
            </li>
          ))}
        </ul>
      ) : (
        <div className="muted small">No step log was recorded for this run.</div>
      )}

      <h3>Evidence cited</h3>
      {r.evidence?.length ? (
        <ul className="small">
          {r.evidence.map((e, i) => (
            <li key={i}>
              <strong>{e.number}</strong> <span className="muted">{e.field}</span>
              <div className="small">{e.snippet}</div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="muted small">None.</div>
      )}

      <h3>Work note</h3>
      <pre className="note">{r.note || ""}</pre>
    </>
  );
}

function ConversationView({ c }: { c: ConvDetail }) {
  const tools = (c.events || []).filter((e) => e.type === "tool");
  const turns = (c.events || []).filter((e) => e.type === "turn" && e.first_token_s != null);
  const avg = turns.length ? (turns.reduce((s, e) => s + (e.first_token_s || 0), 0) / turns.length).toFixed(1) : null;
  return (
    <>
      <h2>{c.summary || "Conversation"}</h2>
      <div className="meta">
        <Outcome value={c.outcome} labels={CONV_LABEL} />
        <Pill>{c.channel === "voice" ? "Voice" : "Text"}</Pill>
        <Pill>{c.user_email || "unknown user"}</Pill>
        {c.incident_number && <Pill>{c.incident_number}</Pill>}
        {c.priority ? <Pill>P{c.priority} to {c.assignment_group}</Pill> : null}
        {avg && <Pill>avg {avg} s to first word</Pill>}
      </div>
      <h3>What the assistant did</h3>
      {tools.length ? (
        <ul className="steps">
          {tools.map((e, i) => {
            const res = e.result || {};
            return (
              <li key={i}>
                <span className="t">{new Date(e.at * 1000).toLocaleTimeString()}</span>
                <strong>{e.name}</strong> {res.status || res.read_back || res.error || ""}{" "}
                <span className="muted">({e.seconds}s)</span>
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="muted small">No searches or tickets.</div>
      )}
      <h3>Transcript</h3>
      <div className="messages" style={{ padding: 0 }}>
        {(c.transcript || []).map((t, i) => (
          <div key={i} className={`bubble ${t.role === "user" ? "user" : "agent"}`}>{t.text}</div>
        ))}
      </div>
    </>
  );
}

// --- tables -------------------------------------------------------------------

function TicketsTab({ open }: { open: (node: ReactNode) => void }) {
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [filter, setFilter] = useState("");
  const [outcome, setOutcome] = useState("");

  useEffect(() => {
    getJSON<RunRow[]>("/api/ops/runs").then(setRuns, console.warn);
  }, []);

  const outcomes = useMemo(() => [...new Set(runs.map((r) => r.outcome))], [runs]);
  const text = filter.trim().toUpperCase();
  const rows = runs.filter((r) => (!text || r.ticket_number.includes(text)) && (!outcome || r.outcome === outcome));

  async function show(id: string) {
    open(<RunView run={await getJSON<RunDetail>(`/api/ops/runs/${encodeURIComponent(id)}`)} />);
  }

  return (
    <section>
      <div className="toolbar">
        <input type="text" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by ticket number" />
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)}>
          <option value="">All outcomes</option>
          {outcomes.map((o) => (
            <option key={o} value={o}>{outcomeName(o)}</option>
          ))}
        </select>
      </div>
      <div className="card">
        <table className="grid">
          <thead>
            <tr><th>Ticket</th><th>Answered</th><th>Outcome</th><th>Confidence</th><th>Took</th><th>Note</th></tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.map((r) => (
                <tr key={r.run_id} onClick={() => void show(r.run_id)}>
                  <td><strong>{r.ticket_number}</strong></td>
                  <td>{when(r.started_at)}</td>
                  <td><Outcome value={r.outcome} labels={OUTCOME_LABEL} /></td>
                  <td>
                    {r.confidence == null ? "-" : (
                      <>{Math.round(r.confidence)} <span className="muted small">{r.confidence_level}</span></>
                    )}
                  </td>
                  <td>{r.latency_s == null ? "-" : `${r.latency_s.toFixed(0)} s`}</td>
                  <td>{r.delivered ? "Posted" : <span className="muted">Dry run</span>}</td>
                </tr>
              ))
            ) : (
              <tr><td colSpan={6} className="empty">No runs recorded yet. They appear here as the agent answers tickets.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ConversationsTab({ open }: { open: (node: ReactNode) => void }) {
  const [rows, setRows] = useState<ConvRow[]>([]);

  useEffect(() => {
    getJSON<ConvRow[]>("/api/ops/conversations").then(setRows, console.warn);
  }, []);

  async function show(id: string) {
    open(<ConversationView c={await getJSON<ConvDetail>(`/api/ops/conversations/${encodeURIComponent(id)}`)} />);
  }

  return (
    <section>
      <div className="card">
        <table className="grid">
          <thead>
            <tr><th>Started</th><th>User</th><th>Channel</th><th>Issue</th><th>Result</th><th>Ticket</th></tr>
          </thead>
          <tbody>
            {rows.length ? (
              rows.map((c) => (
                <tr key={c.id} onClick={() => void show(c.id)}>
                  <td>{when(c.started_at)}</td>
                  <td>{c.user_email || "-"}</td>
                  <td>{c.channel === "voice" ? "Voice" : "Text"}</td>
                  <td>{c.summary || "-"}</td>
                  <td><Outcome value={c.outcome} labels={CONV_LABEL} /></td>
                  <td>{c.incident_number || (c.priority ? `P${c.priority} ${c.assignment_group}` : "-")}</td>
                </tr>
              ))
            ) : (
              <tr><td colSpan={6} className="empty">No chatbot conversations yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// --- page ---------------------------------------------------------------------

const TABS = [
  ["dashboard", "Dashboard"],
  ["tickets", "Tickets answered"],
  ["conversations", "Chatbot conversations"],
] as const;
type Tab = (typeof TABS)[number][0];

function OpsPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [drawer, setDrawer] = useState<ReactNode>(null);

  useEffect(() => {
    getJSON<Me>("/api/me").then(setMe, console.warn);
  }, []);

  if (!me) return null;
  return (
    <>
      <TopBar brand="Ops console" who={me.name || me.email}>
        <a href="/chat">Open the chatbot</a>
      </TopBar>
      <main className="ops">
        <nav className="tabs" role="tablist">
          {TABS.map(([id, label]) => (
            <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>
              {label}
            </button>
          ))}
        </nav>
        {tab === "dashboard" && <DashboardTab />}
        {tab === "tickets" && <TicketsTab open={setDrawer} />}
        {tab === "conversations" && <ConversationsTab open={setDrawer} />}
      </main>
      {drawer && <Drawer onClose={() => setDrawer(null)}>{drawer}</Drawer>}
    </>
  );
}

createRoot(document.getElementById("root")!).render(<OpsPage />);
