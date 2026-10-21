"""Find open tickets that still need the agent, most urgent first.

There is no queue table. ServiceNow already knows which tickets are open and how
urgent they are, and the agent's own notes say which ones it has answered, so
"what needs doing" is a query against ServiceNow. A ticket drops out of that
query by itself the moment its note is posted: nothing has to be removed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .config import Settings
from .notify.formatting import find_previous_note
from .runner import JobRunner, Submit
from .service import fingerprint
from .servicenow.client import ServiceNowClient

log = logging.getLogger(__name__)


@dataclass
class Pending:
    number: str
    table: str
    priority: int
    opened_at: datetime | None
    short_description: str
    reason: str  # "new" or "ticket changed"


def find_pending(
    client: ServiceNowClient, settings: Settings, *, table: str = "incident", stats: dict | None = None
) -> list[Pending]:
    """`stats`, if given, is filled with how many tickets were open and answered."""

    query = "^".join(
        [
            "active=true",
            # Many instances keep a resolved incident active until it auto-closes.
            "resolved_atISEMPTY",
            f"opened_at>=javascript:gs.daysAgoStart({settings.sweep_window_days})",
            "ORDERBYpriority",
            "ORDERBYopened_at",
        ]
    )
    tickets = [
        client.to_ticket(record, table)
        for record in client.query_records(table, query, limit=settings.sweep_max_tickets)
    ]
    if stats is not None:
        stats.update(open=len(tickets), answered=0)
    if not tickets:
        log.info("sweep: no open tickets from the last %d day(s)", settings.sweep_window_days)
        return []

    journals = client.get_journal(table, [t.sys_id for t in tickets], elements=("work_notes",))
    pending: list[Pending] = []
    for ticket in tickets:
        ticket.journal = journals.get(ticket.sys_id, [])
        previous = find_previous_note(ticket.journal)
        if previous and previous.fingerprint == fingerprint(ticket):
            continue  # already answered, and the ticket hasn't changed since
        pending.append(
            Pending(
                number=ticket.number,
                table=table,
                priority=ticket.priority_rank,
                opened_at=ticket.opened_at,
                short_description=ticket.short_description,
                reason="ticket changed" if previous else "new",
            )
        )
    pending.sort(key=lambda p: (p.priority, p.opened_at or datetime.max))
    if stats is not None:
        stats["answered"] = len(tickets) - len(pending)
    log.info(
        "sweep: %d open ticket(s) from the last %d day(s), %d already answered, %d need the agent%s",
        len(tickets),
        settings.sweep_window_days,
        len(tickets) - len(pending),
        len(pending),
        ": " + ", ".join(f"{p.number} (P{p.priority})" for p in pending) if pending else "",
    )
    return pending


def queue_pending(runner: JobRunner, pending: list[Pending]) -> dict[Submit, int]:
    counts: dict[Submit, int] = {}
    for item in pending:
        status = runner.submit(item.number, item.table, item.priority)
        counts[status] = counts.get(status, 0) + 1
    return counts


class Sweeper:
    """Runs the sweep on a timer, so tickets that were already open before the
    agent started, or were missed while it was down, still get picked up."""

    def __init__(self, runner: JobRunner, settings: Settings, client_factory: Callable[[], ServiceNowClient]):
        self.runner = runner
        self.settings = settings
        self.client_factory = client_factory
        # The last sweep, for the ops console.
        self.sweeping = False
        self.last_at: float | None = None
        self.next_at: float | None = None
        self.last_error = ""
        self.last_stats: dict = {}
        self.pending: list[Pending] = []

    def start(self) -> None:
        threading.Thread(target=self._loop, name="sn-agent-sweeper", daemon=True).start()

    def _loop(self) -> None:  # pragma: no cover - timing loop
        client = self.client_factory()
        while True:
            self.sweeping, self.next_at = True, None
            try:
                stats: dict = {}
                pending = find_pending(client, self.settings, stats=stats)
                self.pending, self.last_stats, self.last_error = pending, stats, ""
                counts = queue_pending(self.runner, pending)
                queued = counts.get(Submit.QUEUED, 0)
                if queued:
                    log.info("sweep: queued %d ticket(s), most urgent first", queued)
                for status in (Submit.GAVE_UP, Submit.FULL):
                    if counts.get(status):
                        log.warning("sweep: %d ticket(s) not queued: %s", counts[status], status.value)
            except Exception as exc:
                self.last_error = str(exc)[:300]
                log.exception("sweep FAILED, will try again in %g min: %s", self.settings.sweep_interval_minutes, exc)
            self.sweeping, self.last_at = False, time.time()
            self.next_at = self.last_at + self.settings.sweep_interval_minutes * 60
            time.sleep(self.settings.sweep_interval_minutes * 60)

    def snapshot(self) -> dict:
        return {
            "sweeping": self.sweeping,
            "last_at": self.last_at,
            "next_at": self.next_at,
            "last_error": self.last_error,
            "open": self.last_stats.get("open"),
            "answered": self.last_stats.get("answered"),
            "pending": [
                {
                    "number": p.number,
                    "priority": p.priority,
                    "reason": p.reason,
                    "short_description": p.short_description,
                    "opened_at": p.opened_at.timestamp() if p.opened_at else None,
                }
                for p in self.pending
            ],
            "interval_minutes": self.settings.sweep_interval_minutes,
            "window_days": self.settings.sweep_window_days,
        }
