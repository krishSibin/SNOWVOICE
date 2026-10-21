"""Evidence from a local index of resolved tickets, kept in SQLite.

The live source asks ServiceNow on every search and classifies every ticket it
finds with a model call, which is slow on a voice call and blind to wording
that differs from the query. This index does that work once per ticket, when
`sn-agent index sync` copies it in, and then searches in milliseconds:

1. Ranked keyword search (SQLite FTS5, BM25) over the title, description and
   resolution. Any of the query's words may match, so "laptop can't reach VPN"
   still finds "VPN error 809 from home", which a phrase search would not.
2. The same scoped sweeps as the live source: same configuration item, same
   service, same category.

Text is redacted before it is stored, and the ticket agent's own notes are left
out, so a fix the agent once suggested is never read back as a confirmed one.
Open tickets are not indexed: their state changes by the hour, so duplicate
detection stays live.

KNOWLEDGE_SOURCE picks the source: `live` (the default), `index`, or `hybrid`
(the index first, ServiceNow when the index finds nothing).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from ..config import Settings
from ..ingest.chunking import chunk_ticket
from ..ingest.redact import redact
from ..ingest.resolution_level import classify_resolution_level
from ..llm import BudgetExceeded, LLMClient, LLMOutputError, LLMUnavailable, Meter
from ..models import Candidate, ResolutionLevel, SearchScope, Ticket
from .base import KnowledgeSource

log = logging.getLogger(__name__)

RRF_K = 60
HISTORY_TABLES = ("incident", "problem")
# Re-read this much before the last sync, so a clock or timezone difference
# between here and the instance can never skip a ticket. Re-storing is harmless.
SYNC_OVERLAP = timedelta(days=1)
# Words too common to say anything about which ticket matches.
STOPWORDS = frozenset(
    """a an and are as at be but by can cannot cant could do does doesnt dont for from get gets
    getting had has have having i im in is it its ive me my no not of on or our so that the
    their them then there these they this to too up us was we were what when where which
    while who why will with wont you your""".split()
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_tickets (
    key TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    number TEXT NOT NULL,
    opened_at REAL,
    updated_on REAL,
    category TEXT,
    cmdb_ci_id TEXT,
    business_service TEXT,
    resolution_level TEXT NOT NULL,
    level_checked INTEGER NOT NULL DEFAULT 1,
    indexed_at REAL NOT NULL,
    ticket TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kb_tickets_category ON kb_tickets (category, opened_at);
CREATE INDEX IF NOT EXISTS kb_tickets_ci ON kb_tickets (cmdb_ci_id, opened_at);
CREATE INDEX IF NOT EXISTS kb_tickets_service ON kb_tickets (business_service, opened_at);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_text USING fts5(
    key UNINDEXED, title, body, resolution, tokenize = 'porter unicode61'
);
CREATE TABLE IF NOT EXISTS kb_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# Title words count most, then the fix, then the description.
BM25_WEIGHTS = "0.0, 3.0, 1.0, 2.0"


def _stamp(value: datetime | None) -> float | None:
    return value.timestamp() if value else None


def fts_query(text: str) -> str:
    """Any of the meaningful words, each quoted so punctuation in a ticket
    (an error code, a path) can never break the FTS syntax."""

    words: list[str] = []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) > 1 and word not in STOPWORDS and word not in words:
            words.append(word)
    return " OR ".join(f'"{w}"' for w in words)


def redacted(ticket: Ticket) -> Ticket:
    """The copy that is stored: personal details stripped from every free-text field."""

    copy = ticket.model_copy(deep=True)
    copy.short_description = redact(copy.short_description)
    copy.description = redact(copy.description)
    copy.close_notes = redact(copy.close_notes)
    copy.extra_resolution = {k: redact(v) for k, v in copy.extra_resolution.items()}
    for entry in copy.journal:
        entry.value = redact(entry.value)
    return copy


class TicketIndex:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Shared across the search threads, like Store. WAL lets the voice and
        # web processes read while `index sync` writes in another.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._tx() as cur:
            if self.path != ":memory:":
                cur.execute("PRAGMA journal_mode=WAL")
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

    # --- writes -------------------------------------------------------------

    def upsert(self, ticket: Ticket, level: ResolutionLevel, *, checked: bool = True) -> None:
        stored = redacted(ticket)
        key = f"{stored.table}:{stored.number}"
        resolution = "\n".join(
            [stored.resolution_text, *(j.value for j in stored.journal if j.element == "work_notes")]
        )
        with self._tx() as cur:
            cur.execute(
                """INSERT INTO kb_tickets (key, table_name, number, opened_at, updated_on, category,
                       cmdb_ci_id, business_service, resolution_level, level_checked, indexed_at, ticket)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                       opened_at = excluded.opened_at, updated_on = excluded.updated_on,
                       category = excluded.category, cmdb_ci_id = excluded.cmdb_ci_id,
                       business_service = excluded.business_service,
                       resolution_level = excluded.resolution_level,
                       level_checked = excluded.level_checked,
                       indexed_at = excluded.indexed_at, ticket = excluded.ticket""",
                (
                    key, stored.table, stored.number, _stamp(stored.opened_at), _stamp(stored.updated_on),
                    stored.category, stored.cmdb_ci_id, stored.business_service, level.value,
                    int(checked), time.time(), stored.model_dump_json(),
                ),
            )
            cur.execute("DELETE FROM kb_text WHERE key = ?", (key,))
            cur.execute(
                "INSERT INTO kb_text (key, title, body, resolution) VALUES (?, ?, ?, ?)",
                (key, stored.short_description, stored.description, resolution),
            )

    def remove(self, table: str, number: str) -> None:
        key = f"{table}:{number}"
        with self._tx() as cur:
            cur.execute("DELETE FROM kb_tickets WHERE key = ?", (key,))
            cur.execute("DELETE FROM kb_text WHERE key = ?", (key,))

    def get_meta(self, name: str) -> str | None:
        with self._tx() as cur:
            row = cur.execute("SELECT value FROM kb_meta WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, name: str, value: str) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO kb_meta (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )

    # --- reads --------------------------------------------------------------

    def count(self) -> int:
        with self._tx() as cur:
            return int(cur.execute("SELECT COUNT(*) FROM kb_tickets").fetchone()[0])

    def stats(self) -> dict[str, Any]:
        with self._tx() as cur:
            levels = dict(
                cur.execute("SELECT resolution_level, COUNT(*) FROM kb_tickets GROUP BY resolution_level").fetchall()
            )
            tables = dict(cur.execute("SELECT table_name, COUNT(*) FROM kb_tickets GROUP BY table_name").fetchall())
            unchecked = cur.execute("SELECT COUNT(*) FROM kb_tickets WHERE level_checked = 0").fetchone()[0]
        return {
            "tickets": sum(tables.values()),
            "by_table": tables,
            "by_level": levels,
            "unchecked": int(unchecked),
            "last_sync": self.get_meta("last_sync"),
        }

    def unchecked(self, limit: int) -> list[Ticket]:
        with self._tx() as cur:
            rows = cur.execute("SELECT ticket FROM kb_tickets WHERE level_checked = 0 LIMIT ?", (limit,)).fetchall()
        return [Ticket.model_validate_json(r["ticket"]) for r in rows]

    def _filters(self, exclude: str, since: datetime | None, before: datetime | None) -> tuple[str, list[Any]]:
        sql = " AND t.key != ?"
        args: list[Any] = [exclude]
        if since:
            sql += " AND t.opened_at >= ?"
            args.append(since.timestamp())
        if before:
            sql += " AND t.opened_at < ?"
            args.append(before.timestamp())
        return sql, args

    def search_text(
        self, text: str, *, exclude: str = "", since: datetime | None = None,
        before: datetime | None = None, limit: int = 10,
    ) -> list[str]:
        """Keys of the best matches, best first."""

        match = fts_query(text)
        if not match:
            return []
        where, args = self._filters(exclude, since, before)
        with self._tx() as cur:
            rows = cur.execute(
                f"""SELECT t.key FROM kb_text JOIN kb_tickets t ON t.key = kb_text.key
                    WHERE kb_text MATCH ?{where}
                    ORDER BY bm25(kb_text, {BM25_WEIGHTS}) LIMIT ?""",
                [match, *args, limit],
            ).fetchall()
        return [r["key"] for r in rows]

    def by_field(
        self, field: str, value: str, *, exclude: str = "", since: datetime | None = None,
        before: datetime | None = None, limit: int = 15,
    ) -> list[str]:
        """Keys of the most recent tickets with this category, CI or service."""

        column = {"category": "category", "cmdb_ci": "cmdb_ci_id", "business_service": "business_service"}[field]
        where, args = self._filters(exclude, since, before)
        with self._tx() as cur:
            rows = cur.execute(
                f"SELECT t.key FROM kb_tickets t WHERE t.{column} = ?{where} ORDER BY t.opened_at DESC LIMIT ?",
                [value, *args, limit],
            ).fetchall()
        return [r["key"] for r in rows]

    def load(self, keys: list[str]) -> dict[str, tuple[Ticket, ResolutionLevel]]:
        if not keys:
            return {}
        marks = ",".join("?" * len(keys))
        with self._tx() as cur:
            rows = cur.execute(
                f"SELECT key, resolution_level, ticket FROM kb_tickets WHERE key IN ({marks})", keys
            ).fetchall()
        return {
            r["key"]: (Ticket.model_validate_json(r["ticket"]), ResolutionLevel(r["resolution_level"])) for r in rows
        }


class IndexSearchSource:
    """KnowledgeSource over the local index. Open tickets are still found live."""

    name = "local_index"

    def __init__(
        self, index: TicketIndex, settings: Settings, live: KnowledgeSource | None = None,
        *, fall_back_to_live: bool = False,
    ):
        self.index = index
        self.settings = settings
        self.live = live
        self.fall_back_to_live = fall_back_to_live

    def search(
        self, *, ticket: Ticket, queries: list[str], scope: SearchScope, limit: int, meter: Meter
    ) -> list[Candidate]:
        exclude = f"{ticket.table}:{ticket.number}"
        since = datetime.now(timezone.utc) - timedelta(days=scope.recency_days) if scope.recency_days else None
        window = {"exclude": exclude, "since": since, "before": scope.as_of}

        ranked: list[tuple[list[str], float]] = []
        for query in queries:
            keys = self.index.search_text(query, limit=self.settings.search_results_per_query, **window)
            log.debug('   index full-text "%s": %d hit(s)', query, len(keys))
            ranked.append((keys, 1.0))
        for field, value in self._sweeps(ticket, scope):
            keys = self.index.by_field(field, value, limit=self.settings.search_sweep_limit, **window)
            log.debug("   index sweep by %s: %d hit(s)", field, len(keys))
            # Same component is weaker evidence than similar wording, as live.
            ranked.append((keys, 0.6))

        candidates = self._to_candidates(ranked, limit)
        if not candidates and self.fall_back_to_live and self.live is not None:
            log.info("   nothing in the local index; searching ServiceNow live")
            return self.live.search(ticket=ticket, queries=queries, scope=scope, limit=limit, meter=meter)
        return candidates

    def find_open(self, ticket: Ticket, queries: list[str], meter: Meter) -> list[Candidate]:
        return self.live.find_open(ticket, queries, meter) if self.live is not None else []

    @staticmethod
    def _sweeps(ticket: Ticket, scope: SearchScope) -> list[tuple[str, str]]:
        if scope.level >= 2:
            return []
        sweeps: list[tuple[str, str]] = []
        if scope.level == 0 and ticket.cmdb_ci_id:
            sweeps.append(("cmdb_ci", ticket.cmdb_ci_id))
        if scope.level == 0 and ticket.business_service:
            sweeps.append(("business_service", ticket.business_service))
        if ticket.category:
            sweeps.append(("category", ticket.category))
        return sweeps

    def _to_candidates(self, ranked: list[tuple[list[str], float]], limit: int) -> list[Candidate]:
        scores: dict[str, float] = defaultdict(float)
        for keys, weight in ranked:
            for rank, key in enumerate(keys):
                scores[key] += weight / (RRF_K + rank + 1)
        top = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        stored = self.index.load([key for key, _score in top])
        candidates: list[Candidate] = []
        for key, score in top:
            if key not in stored:
                continue
            ticket, level = stored[key]
            # The resolution level was decided at sync time: no model call here.
            chunks = chunk_ticket(ticket, level, max_chars=self.settings.chunk_max_chars)
            if chunks:
                candidates.append(Candidate.from_chunks(chunks, score))
        return candidates


def knowledge_source(settings: Settings, live: Any) -> Any:
    """The source KNOWLEDGE_SOURCE asks for, wrapping the live one it is given."""

    if settings.knowledge_source == "live":
        return live
    index = TicketIndex(settings.app_db_path)
    if index.count() == 0:
        log.warning("the local ticket index is empty; fill it with: sn-agent index sync")
    return IndexSearchSource(index, settings, live, fall_back_to_live=settings.knowledge_source == "hybrid")


# --- sync -------------------------------------------------------------------


@dataclass
class SyncReport:
    stored: int = 0
    removed: int = 0
    unchecked: int = 0
    rechecked: int = 0


def _history_query(table: str, since: datetime | None, days: int) -> str:
    clauses = (
        ["resolved_atISNOTEMPTY"]
        if table == "incident"
        else ["fix_notesISNOTEMPTY^ORcause_notesISNOTEMPTY^ORworkaroundISNOTEMPTY"]
    )
    if since:
        stamp = (since - SYNC_OVERLAP).strftime("%Y-%m-%d")
        clauses.append(f"sys_updated_on>=javascript:gs.dateGenerate('{stamp}','00:00:00')")
    else:
        clauses.append(f"opened_at>=javascript:gs.daysAgoStart({days})")
    clauses.append("ORDERBYsys_updated_on")
    return "^".join(clauses)


def sync_index(
    index: TicketIndex, client: Any, settings: Settings, llm: LLMClient | None, *,
    full: bool = False, days: int | None = None, recheck_limit: int = 50,
) -> SyncReport:
    """Copies resolved tickets from ServiceNow into the index. The first run (or
    `full`) reads the last `days`; later runs only what changed since the last.

    `client` must leave the ticket agent's own notes out of its journal reads
    (HumanNotesOnly). The model decides a resolution level only where the
    rules cannot; if it is unavailable the safe rule-based level is stored and
    the ticket is checked again on the next sync."""

    report = SyncReport()
    days = days or settings.search_recency_days
    meter = Meter(settings.prices, max_cost_usd=float("inf"), max_tokens=10**12)
    started = datetime.now(timezone.utc)

    def classify(ticket: Ticket) -> tuple[ResolutionLevel, bool]:
        try:
            return classify_resolution_level(ticket, llm=llm, meter=meter), llm is not None
        except (LLMUnavailable, LLMOutputError, BudgetExceeded) as exc:
            log.warning("could not check %s with the model (%s); will retry next sync", ticket.number, exc)
            return classify_resolution_level(ticket), False

    for table in HISTORY_TABLES:
        last = None if full else index.get_meta(f"synced:{table}")
        since = datetime.fromisoformat(last) if last else None
        query = _history_query(table, since, days)
        log.info("index sync: reading %s %s", table, f"changed since {since:%Y-%m-%d}" if since else f"from the last {days} days")
        batch: list[Ticket] = []

        def flush() -> None:
            journals = client.get_journal(table, [t.sys_id for t in batch])
            for ticket in batch:
                ticket.journal = journals.get(ticket.sys_id, [])
                if ticket.active and table == "incident":
                    index.remove(table, ticket.number)  # reopened since it was stored
                    report.removed += 1
                    continue
                level, checked = classify(ticket)
                index.upsert(ticket, level, checked=checked)
                report.stored += 1
                report.unchecked += not checked
            log.info("index sync: %d ticket(s) stored so far", report.stored)
            batch.clear()

        for record in client.query_records(table, query):
            ticket = client.to_ticket(record, table)
            if ticket.number:
                batch.append(ticket)
            if len(batch) >= 50:
                flush()
        if batch:
            flush()
        index.set_meta(f"synced:{table}", started.isoformat())

    for ticket in index.unchecked(recheck_limit):
        level, checked = classify(ticket)
        if checked:
            index.upsert(ticket, level, checked=True)
            report.rechecked += 1

    index.set_meta("last_sync", started.isoformat())
    return report
