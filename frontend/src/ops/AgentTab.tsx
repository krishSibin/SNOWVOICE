// The live view of `sn-agent serve`: whether it is running, what each worker is
// on, which tickets still need the agent, how the last results went, and its
// log as it is written. Polls the backend every two seconds while open.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getJSON, when } from "../shared/api";
import { Pill, type Tone } from "../shared/ui";

const POLL_MS = 2000;
const MAX_LINES = 1500;

// --- API shape ----------------------------------------------------------------

interface Job {
  id: number;
  ticket_number: string;
  at: number;
  status: string; // posted | printed | skipped | failed
  priority: number | null;
  outcome: string;
  confidence: number | null;
  cost_usd: number | null;
  detail: string;
}
interface LogLine {
  id: number;
  ticket_number: string;
  at: number;
  level: string;
  message: string;
}
interface PendingTicket {
  number: string;
  priority: number;
  reason: string;
  short_description: string;
  opened_at: number | null;
}
interface AgentStatus {
  alive: boolean;
  phase: "working" | "sweeping" | "idle" | "stopped";
  heartbeat_at: number;
  heartbeat_age_s: number;
  host: string;
  pid: number;
  mode: string;
  started_at: number;
  runner: {
    workers: number;
    working: { number: string; since: number }[];
    queued: { number: string; priority: number }[];
    gave_up: string[];
    totals: Record<string, number>;
  };
  sweep: {
    sweeping: boolean;
    last_at: number | null;
    next_at: number | null;
    last_error: string;
    open: number | null;
    answered: number | null;
    pending: PendingTicket[];
    interval_minutes: number;
    window_days: number;
  } | null;
  config: { models: string[]; knowledge_source: string; max_failures: number };
}
interface Live {
  now: number;
  status: AgentStatus | null;
  counts: { day: Record<string, number>; all: Record<string, number> };
  recent: Job[];
  log: LogLine[];
  instance_url: string;
}

// --- helpers ------------------------------------------------------------------

const JOB_LABEL: Record<string, [string, Tone]> = {
  posted: ["Note posted", "good"],
  printed: ["Dry run", ""],
  skipped: ["Skipped", ""],
  failed: ["Failed", "bad"],
};

function span(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d`;
}

function ticketUrl(instance: string, number: string): string {
  const table = number.startsWith("PRB") ? "problem" : "incident";
  return `${instance}/nav_to.do?uri=${encodeURIComponent(`${table}.do?sysparm_query=number=${number}`)}`;
}

function TicketLink({ instance, number }: { instance: string; number: string }) {
  if (!instance) return <strong>{number}</strong>;
  return (
    <a href={ticketUrl(instance, number)} target="_blank" rel="noreferrer">
      <strong>{number}</strong>
    </a>
  );
}

function headline(s: AgentStatus | null, now: number): [string, string] {
  if (!s) return ["Not started yet", "Start it with: sn-agent serve"];
  if (!s.alive) {
    return [
      "Stopped",
      s.started_at ? `last seen ${span(now - s.heartbeat_at)} ago. Start it again with: sn-agent serve` : "",
    ];
  }
  const working = s.runner.working;
  if (s.phase === "working") {
    const first = working[0];
    const more = working.length > 1 ? ` and ${working.length - 1} more` : "";
    return [`Working on ${first.number}${more}`, `for ${span(now - first.since)}`];
  }
  if (s.phase === "sweeping") return ["Checking ServiceNow for new tickets", ""];
  const next = s.sweep?.next_at;
  return ["Idle", next ? `next check in ${span(next - now)}` : "waiting for the first check"];
}

// --- the tab ------------------------------------------------------------------

export function AgentTab() {
  const [live, setLive] = useState<Live | null>(null);
  const [lines, setLines] = useState<LogLine[]>([]);
  const [error, setError] = useState("");
  const lastId = useRef(0);

  useEffect(() => {
    let stopped = false;
    let timer = 0;
    async function poll() {
      if (!document.hidden) {
        try {
          const data = await getJSON<Live>(`/api/ops/agent?after=${lastId.current}`);
          if (stopped) return;
          setLive(data);
          setError("");
          if (data.log.length) {
            lastId.current = data.log[data.log.length - 1].id;
            setLines((old) => [...old, ...data.log].slice(-MAX_LINES));
          }
        } catch (err) {
          if (!stopped) setError(String(err));
        }
      }
      if (!stopped) timer = window.setTimeout(poll, POLL_MS);
    }
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, []);

  if (!live) return <section className="muted">{error || "Loading..."}</section>;

  const s = live.status;
  const now = live.now;
  const [title, sub] = headline(s, now);
  const day = live.counts.day;
  const pending = s?.alive ? s.sweep?.pending ?? [] : [];

  return (
    <section className="agent">
      <div className={`card agent-head ${s?.alive ? s.phase : "stopped"}`}>
        <span className="phase-dot" aria-hidden="true" />
        <div className="agent-title">
          <div className="big">{title}</div>
          <div className="muted small">{sub}</div>
        </div>
        {s && (
          <dl className="agent-facts small">
            <div><dt>Running on</dt><dd>{s.host} (pid {s.pid})</dd></div>
            <div><dt>Up for</dt><dd>{s.alive ? span(now - s.started_at) : "-"}</dd></div>
            <div><dt>Model</dt><dd>{s.config.models[1]}</dd></div>
            <div><dt>Searches</dt><dd>{s.config.knowledge_source === "live" ? "ServiceNow live" : `local index (${s.config.knowledge_source})`}</dd></div>
          </dl>
        )}
        {error && <div className="error">Lost contact with the web app: {error}</div>}
      </div>

      <div className="kpis">
        <Kpi label="Notes posted" value={day.posted ?? 0} sub={`last 24 h · ${live.counts.all.posted ?? 0} in total`} />
        <Kpi label="Failed" value={day.failed ?? 0} tone={day.failed ? "bad" : ""}
          sub={s?.runner.gave_up.length ? `${s.runner.gave_up.length} given up on until restart` : "last 24 h"} />
        <Kpi label="Skipped or dry run" value={(day.skipped ?? 0) + (day.printed ?? 0)} sub="last 24 h" />
        <Kpi label="Waiting for the agent" value={s?.alive ? pending.length : "-"}
          sub={s?.alive && s.sweep?.open != null ? `of ${s.sweep.open} open in the last ${s.sweep.window_days} day(s)` : "from the last check"} />
        <Kpi label="Workers busy" value={s?.alive ? `${s.runner.working.length} / ${s.runner.workers}` : "-"}
          sub={s?.alive ? `${s.runner.queued.length} queued` : ""} />
      </div>

      {s?.sweep?.last_error && s.alive && (
        <div className="card agent-alert">Last check of ServiceNow failed: {s.sweep.last_error}</div>
      )}

      <div className="agent-cols">
        <PendingCard status={s} pending={pending} recent={live.recent} now={now} instance={live.instance_url} />
        <RecentCard jobs={live.recent} instance={live.instance_url} />
      </div>

      <LogPanel lines={lines} />
    </section>
  );
}

function Kpi({ label, value, sub, tone = "" }: { label: string; value: ReactNode; sub?: string; tone?: Tone }) {
  return (
    <div className={`card kpi ${tone}`}>
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      <div className="sub">{sub}</div>
    </div>
  );
}

function PendingCard({ status, pending, recent, now, instance }: {
  status: AgentStatus | null;
  pending: PendingTicket[];
  recent: Job[];
  now: number;
  instance: string;
}) {
  const working = new Map(status?.runner.working.map((w) => [w.number, w.since]));
  const queued = new Set(status?.runner.queued.map((q) => q.number));
  const gaveUp = new Set(status?.runner.gave_up);
  // Results since the sweep that listed these tickets; newest first, so the latest wins.
  const since = status?.sweep?.last_at ?? 0;
  const latest = new Map<string, Job>();
  for (const j of recent) if (j.at >= since && !latest.has(j.ticket_number)) latest.set(j.ticket_number, j);

  function state(number: string): [string, Tone] {
    if (working.has(number)) return [`Working · ${span(now - (working.get(number) ?? now))}`, "warn"];
    const done = latest.get(number);
    if (done && done.status !== "failed") return JOB_LABEL[done.status] || [done.status, ""];
    if (gaveUp.has(number)) return ["Gave up", "bad"];
    if (queued.has(number)) return ["Queued", ""];
    if (done) return ["Failed, will retry", "bad"];
    return ["Next check", ""];
  }

  return (
    <div className="card agent-card">
      <h3>Tickets that need the agent</h3>
      <table className="grid static">
        <thead>
          <tr><th>Ticket</th><th>Pri</th><th>Issue</th><th>State</th></tr>
        </thead>
        <tbody>
          {pending.length ? (
            pending.map((p) => {
              const [label, tone] = state(p.number);
              return (
                <tr key={p.number}>
                  <td><TicketLink instance={instance} number={p.number} /></td>
                  <td>P{p.priority}</td>
                  <td>
                    {p.short_description || "-"}
                    {p.reason !== "new" && <div className="muted small">{p.reason}</div>}
                  </td>
                  <td><Pill tone={tone}>{label}</Pill></td>
                </tr>
              );
            })
          ) : (
            <tr><td colSpan={4} className="empty">
              {status?.alive ? "Nothing waiting: every recent open ticket has a current note." : "Shown while the agent is running."}
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function RecentCard({ jobs, instance }: { jobs: Job[]; instance: string }) {
  return (
    <div className="card agent-card">
      <h3>Latest results</h3>
      <table className="grid static">
        <thead>
          <tr><th>Ticket</th><th>Result</th><th>Confidence</th><th>When</th></tr>
        </thead>
        <tbody>
          {jobs.length ? (
            jobs.map((j) => {
              const [label, tone] = JOB_LABEL[j.status] || [j.status, ""];
              return (
                <tr key={j.id}>
                  <td><TicketLink instance={instance} number={j.ticket_number} /></td>
                  <td>
                    <Pill tone={tone}>{label}</Pill>
                    {j.detail && <div className="muted small clip" title={j.detail}>{j.detail}</div>}
                  </td>
                  <td>{j.confidence == null ? "-" : Math.round(j.confidence)}</td>
                  <td className="small">{when(j.at)}</td>
                </tr>
              );
            })
          ) : (
            <tr><td colSpan={4} className="empty">No results yet.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function LogPanel({ lines }: { lines: LogLine[] }) {
  const [ticket, setTicket] = useState("");
  const [problems, setProblems] = useState(false);
  const [follow, setFollow] = useState(true);
  const box = useRef<HTMLDivElement>(null);

  const shown = useMemo(
    () =>
      lines.filter(
        (l) =>
          (!ticket || l.ticket_number === ticket) &&
          (!problems || l.level === "WARNING" || l.level === "ERROR" || l.level === "CRITICAL"),
      ),
    [lines, ticket, problems],
  );

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [shown, follow]);

  function onScroll() {
    const el = box.current;
    if (el) setFollow(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  }

  return (
    <div className="card agent-log">
      <div className="log-bar">
        <h3>Live log</h3>
        <label className="small"><input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} /> Warnings and errors only</label>
        {ticket && (
          <button type="button" className="small" onClick={() => setTicket("")}>Showing {ticket} only ✕</button>
        )}
        <span className="spacer" />
        {!follow && (
          <button type="button" className="small" onClick={() => setFollow(true)}>Jump to latest</button>
        )}
      </div>
      <div className="log" ref={box} onScroll={onScroll} role="log" aria-live="off">
        {shown.length ? (
          shown.map((l) => (
            <div key={l.id} className={`line ${l.level.toLowerCase()}`}>
              <span className="t">{new Date(l.at * 1000).toLocaleTimeString()}</span>
              {l.ticket_number ? (
                <button type="button" className="tag" onClick={() => setTicket(l.ticket_number)} title="Show only this ticket">
                  {l.ticket_number}
                </button>
              ) : (
                <span className="tag none">agent</span>
              )}
              <span className="m">{l.message}</span>
            </div>
          ))
        ) : (
          <div className="muted small">No log lines yet. They appear here while sn-agent serve runs.</div>
        )}
      </div>
    </div>
  );
}
