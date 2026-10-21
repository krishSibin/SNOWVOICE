"""Local record of logins, intake conversations and ticket-agent runs.

The ticket agent itself still keeps nothing: ServiceNow work notes remain its
record. This file exists for the web app: the ops console needs each run's steps
and the dashboard needs counts over time, and neither can be read back out of a
work note. One SQLite file, no server.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('user', 'ops')),
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_email TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'text',
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'open',
    summary TEXT NOT NULL DEFAULT '',
    incident_number TEXT NOT NULL DEFAULT '',
    priority INTEGER,
    assignment_group TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '[]',
    events TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS conversations_started ON conversations (started_at);
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    ticket_number TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT NOT NULL DEFAULT '',
    confidence REAL,
    confidence_level TEXT NOT NULL DEFAULT '',
    latency_s REAL,
    delivered INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS agent_runs_ticket ON agent_runs (ticket_number);
CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_number TEXT NOT NULL,
    at REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_steps_ticket ON agent_steps (ticket_number, at);
-- What `sn-agent serve` is doing right now, rewritten every few seconds.
CREATE TABLE IF NOT EXISTS agent_status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    heartbeat_at REAL NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
-- Every ticket a worker finished, including the ones that failed.
CREATE TABLE IF NOT EXISTS agent_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_number TEXT NOT NULL,
    at REAL NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER,
    outcome TEXT NOT NULL DEFAULT '',
    confidence REAL,
    cost_usd REAL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS agent_jobs_at ON agent_jobs (at);
"""

# Conversation outcomes. "solved" is the one the dashboard counts as deflected.
OUTCOMES = ("open", "solved", "ticket_raised", "ticket_drafted", "abandoned")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared across threads, guarded by a lock: writes are
        # small and rare, and this keeps an in-memory database usable in tests.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._tx() as cur:
            cur.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._tx() as cur:
            return [dict(row) for row in cur.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    # --- users --------------------------------------------------------------

    def put_user(self, email: str, name: str, role: str, password_hash: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO users (email, name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET name=excluded.name, role=excluded.role, "
                "password_hash=excluded.password_hash",
                (email.lower().strip(), name, role, password_hash, time.time()),
            )

    def get_user(self, email: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))

    def list_users(self) -> list[dict[str, Any]]:
        return self._all("SELECT email, name, role, created_at FROM users ORDER BY role, email")

    def delete_user(self, email: str) -> bool:
        with self._tx() as cur:
            return cur.execute("DELETE FROM users WHERE email = ?", (email.lower().strip(),)).rowcount > 0

    # --- conversations ------------------------------------------------------

    def save_conversation(self, conv: dict[str, Any]) -> None:
        now = time.time()
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO conversations (id, user_email, channel, started_at, updated_at, outcome, summary, "
                "incident_number, priority, assignment_group, transcript, events) "
                "VALUES (:id, :user_email, :channel, :started_at, :updated_at, :outcome, :summary, "
                ":incident_number, :priority, :assignment_group, :transcript, :events) "
                "ON CONFLICT(id) DO UPDATE SET channel=excluded.channel, updated_at=excluded.updated_at, "
                "outcome=excluded.outcome, summary=excluded.summary, incident_number=excluded.incident_number, "
                "priority=excluded.priority, assignment_group=excluded.assignment_group, "
                "transcript=excluded.transcript, events=excluded.events",
                {
                    "id": conv["id"],
                    "user_email": conv.get("user_email", ""),
                    "channel": conv.get("channel", "text"),
                    "started_at": conv.get("started_at", now),
                    "updated_at": now,
                    "outcome": conv.get("outcome", "open"),
                    "summary": conv.get("summary", ""),
                    "incident_number": conv.get("incident_number", ""),
                    "priority": conv.get("priority"),
                    "assignment_group": conv.get("assignment_group", ""),
                    "transcript": json.dumps(conv.get("transcript", [])),
                    "events": json.dumps(conv.get("events", []), default=str),
                },
            )

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM conversations WHERE id = ?", (conv_id,))
        return _decode(row, "transcript", "events") if row else None

    def list_conversations(self, *, user_email: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, user_email, channel, started_at, updated_at, outcome, summary, incident_number, "
            "priority, assignment_group FROM conversations"
        )
        args: tuple = ()
        if user_email is not None:
            sql += " WHERE user_email = ?"
            args = (user_email.lower().strip(),)
        return self._all(sql + " ORDER BY started_at DESC LIMIT ?", (*args, limit))

    # --- ticket-agent runs --------------------------------------------------

    def save_run(self, result: dict[str, Any], *, delivered: bool) -> None:
        confidence = result.get("confidence") or {}
        latency = float(result.get("latency_s") or 0.0)
        finished = time.time()
        with self._tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO agent_runs (run_id, ticket_number, started_at, finished_at, outcome, "
                "confidence, confidence_level, latency_s, delivered, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result["run_id"],
                    result.get("ticket_number", ""),
                    finished - latency,
                    finished,
                    str(result.get("outcome", "")),
                    confidence.get("score"),
                    confidence.get("level", ""),
                    latency,
                    1 if delivered else 0,
                    json.dumps(result, default=str),
                ),
            )

    def list_runs(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._all(
            "SELECT run_id, ticket_number, started_at, finished_at, outcome, confidence, confidence_level, "
            "latency_s, delivered, json_extract(result, '$.table') AS tbl FROM agent_runs "
            "ORDER BY started_at DESC LIMIT ?",
            (limit,),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        if not row:
            return None
        row = _decode(row, "result")
        # The step log lines written while this run was in flight.
        row["steps"] = self._all(
            "SELECT at, level, message FROM agent_steps WHERE ticket_number = ? AND at >= ? AND at <= ? "
            "ORDER BY at, id",
            (row["ticket_number"], row["started_at"] - 2, (row["finished_at"] or time.time()) + 2),
        )
        return row

    def add_step(self, ticket_number: str, level: str, message: str, at: float | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO agent_steps (ticket_number, at, level, message) VALUES (?, ?, ?, ?)",
                (ticket_number, at or time.time(), level, message),
            )

    def steps_after(self, after_id: int = 0, *, limit: int = 300) -> list[dict[str, Any]]:
        """Log lines newer than `after_id`, oldest first. With no id, the latest `limit`."""

        if after_id:
            return self._all(
                "SELECT id, ticket_number, at, level, message FROM agent_steps WHERE id > ? ORDER BY id LIMIT ?",
                (after_id, limit),
            )
        rows = self._all(
            "SELECT id, ticket_number, at, level, message FROM agent_steps ORDER BY id DESC LIMIT ?", (limit,)
        )
        return rows[::-1]

    # --- the running agent --------------------------------------------------

    def put_agent_status(self, data: dict[str, Any]) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO agent_status (id, heartbeat_at, data) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at, data = excluded.data",
                (time.time(), json.dumps(data)),
            )

    def get_agent_status(self) -> dict[str, Any] | None:
        row = self._one("SELECT heartbeat_at, data FROM agent_status WHERE id = 1")
        if not row:
            return None
        return {**json.loads(row["data"]), "heartbeat_at": row["heartbeat_at"]}

    def add_job(self, job: dict[str, Any]) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO agent_jobs (ticket_number, at, status, priority, outcome, confidence, cost_usd, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["number"], job.get("at") or time.time(), job["status"], job.get("priority"),
                    job.get("outcome") or "", job.get("confidence"), job.get("cost_usd"), job.get("detail") or "",
                ),
            )

    def job_counts(self, since: float = 0.0) -> dict[str, int]:
        rows = self._all("SELECT status, COUNT(*) AS n FROM agent_jobs WHERE at >= ? GROUP BY status", (since,))
        return {r["status"]: r["n"] for r in rows}

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM agent_jobs ORDER BY id DESC LIMIT ?", (limit,))

    # --- dashboard ----------------------------------------------------------

    def dashboard(self, days: int = 30) -> dict[str, Any]:
        since = time.time() - days * 86400
        convs = self._all(
            "SELECT outcome, priority, assignment_group, channel, events FROM conversations WHERE started_at >= ?",
            (since,),
        )
        runs = self._all(
            "SELECT started_at, outcome, confidence, latency_s FROM agent_runs WHERE started_at >= ?", (since,)
        )
        closed = [c for c in convs if c["outcome"] != "open"]
        solved = sum(1 for c in closed if c["outcome"] == "solved")
        raised = [c for c in closed if c["outcome"] in {"ticket_raised", "ticket_drafted"}]

        turn_latencies: list[float] = []
        for conv in convs:
            for event in json.loads(conv["events"] or "[]"):
                if event.get("type") == "turn" and event.get("first_token_s") is not None:
                    turn_latencies.append(float(event["first_token_s"]))

        per_day: dict[str, int] = {}
        for run in runs:
            day = time.strftime("%Y-%m-%d", time.localtime(run["started_at"]))
            per_day[day] = per_day.get(day, 0) + 1

        return {
            "days": days,
            "conversations": len(convs),
            "closed_conversations": len(closed),
            "solved_without_ticket": solved,
            "deflection_rate": round(solved / len(closed), 3) if closed else None,
            "tickets_raised": len(raised),
            "raised_by_priority": _count(str(c["priority"] or "?") for c in raised),
            "raised_by_group": _count(c["assignment_group"] or "?" for c in raised),
            "by_channel": _count(c["channel"] for c in convs),
            "runs": len(runs),
            "runs_per_day": dict(sorted(per_day.items())),
            "run_outcomes": _count(r["outcome"] for r in runs),
            "confidence_histogram": _histogram([r["confidence"] for r in runs if r["confidence"] is not None]),
            "run_latency_s": _percentiles([r["latency_s"] for r in runs if r["latency_s"] is not None]),
            "turn_first_token_s": _percentiles(turn_latencies),
        }


def _decode(row: dict[str, Any], *fields: str) -> dict[str, Any]:
    for field in fields:
        row[field] = json.loads(row.get(field) or "null")
    return row


def _count(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[value] = out.get(value, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _histogram(scores: list[float]) -> dict[str, int]:
    bins = {f"{lo}-{lo + 9}": 0 for lo in range(0, 100, 10)}
    for score in scores:
        lo = min(90, max(0, int(score // 10) * 10))
        bins[f"{lo}-{lo + 9}"] += 1
    return bins


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p95": None, "count": 0}
    ordered = sorted(values)

    def pick(q: float) -> float:
        return round(ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))], 2)

    return {"p50": pick(0.5), "p95": pick(0.95), "count": len(ordered)}
