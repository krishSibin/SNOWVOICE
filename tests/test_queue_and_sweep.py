"""The priority queue, and the sweep that treats ServiceNow as the queue."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sn_agent.agent.schemas import Outcome, RunResult
from sn_agent.config import Settings
from sn_agent.models import JournalEntry
from sn_agent.notify.formatting import render_note
from sn_agent.runner import JobRunner, Submit
from sn_agent.service import fingerprint
from sn_agent.sweep import find_pending, queue_pending

from .factories import NOW, make_ticket


class RecordingService:
    """Stands in for ResolutionService and records the order it was asked in."""

    def __init__(self, fail: set[str] | None = None):
        self.order: list[str] = []
        self.fail = fail or set()

    def resolve(self, number, table="incident"):
        self.order.append(number)
        if number in self.fail:
            raise RuntimeError("boom")
        result = RunResult(run_id="r", ticket_number=number, ticket_sys_id="s", outcome=Outcome.NO_HISTORY)
        return SimpleNamespace(result=result, published=SimpleNamespace(delivered=True), skipped_reason="")


def test_most_urgent_ticket_is_worked_first_whatever_the_arrival_order():
    service = RecordingService()
    runner = JobRunner(workers=1, service_factory=lambda: service)

    # Submitted before the worker starts, so ordering is decided by the queue alone.
    runner.submit("INC_P4", priority=4)
    runner.submit("INC_P1", priority=1)
    runner.submit("INC_P3_first", priority=3)
    runner.submit("INC_P3_second", priority=3)
    runner.start()
    runner.wait()

    assert service.order == ["INC_P1", "INC_P3_first", "INC_P3_second", "INC_P4"]


def test_a_ticket_already_queued_is_not_queued_twice():
    runner = JobRunner(workers=1, service_factory=RecordingService)

    assert runner.submit("INC0100") == Submit.QUEUED
    assert runner.submit("INC0100") == Submit.ALREADY_QUEUED


def test_a_ticket_that_keeps_failing_is_given_up_on():
    """Otherwise every sweep would retry it forever and pay for it each time."""

    service = RecordingService(fail={"INC_BAD"})
    runner = JobRunner(workers=1, service_factory=lambda: service, max_failures=2)
    runner.start()

    for _ in range(2):
        assert runner.submit("INC_BAD") == Submit.QUEUED
        runner.wait()

    assert runner.submit("INC_BAD") == Submit.GAVE_UP
    assert [r.status for r in runner.results] == ["failed", "failed"]


class SweepInstance:
    """Answers the sweep's two questions: which tickets are open, and what notes
    do they carry."""

    def __init__(self, tickets):
        self.tickets = tickets
        self.query = ""

    def query_records(self, table, query, *, limit=None, **_):
        self.query = query
        open_tickets = [t for t in self.tickets if t.active]
        open_tickets.sort(key=lambda t: (t.priority_rank, t.opened_at))
        return [{"_ticket": t} for t in open_tickets[:limit]]

    def to_ticket(self, record, table):
        return record["_ticket"]

    def get_journal(self, table, sys_ids, elements=("work_notes",)):
        return {t.sys_id: t.journal for t in self.tickets if t.sys_id in set(sys_ids)}


def _answered(ticket):
    """Leaves the note the agent would have posted on this exact ticket text."""

    result = RunResult(run_id="run-1", ticket_number=ticket.number, ticket_sys_id=ticket.sys_id)
    note = render_note(result, fingerprint=fingerprint(ticket))
    ticket.journal.append(JournalEntry(element="work_notes", value=note, created_on=NOW))
    return ticket


def test_sweep_finds_unanswered_open_tickets_most_urgent_first():
    p4 = make_ticket("INC_P4", active=True, days_ago=1)
    p4.priority = "4 - Low"
    p1 = make_ticket("INC_P1", active=True, days_ago=0)
    p1.priority = "1 - Critical"
    done = _answered(make_ticket("INC_DONE", active=True, days_ago=0))
    done.priority = "1 - Critical"
    closed = make_ticket("INC_CLOSED", active=False)

    pending = find_pending(SweepInstance([p4, p1, done, closed]), Settings())

    assert [p.number for p in pending] == ["INC_P1", "INC_P4"]
    assert [p.reason for p in pending] == ["new", "new"]


def test_a_rewritten_ticket_comes_back_into_the_queue():
    ticket = _answered(make_ticket("INC0100", active=True, days_ago=0))
    ticket.description = "Actually it is only the second floor, and only on Wi-Fi."

    pending = find_pending(SweepInstance([ticket]), Settings())

    assert [(p.number, p.reason) for p in pending] == [("INC0100", "ticket changed")]


def test_sweep_asks_servicenow_for_open_recent_tickets_by_priority():
    instance = SweepInstance([])

    find_pending(instance, Settings(sweep_window_days=3))

    assert "active=true" in instance.query
    assert "resolved_atISEMPTY" in instance.query
    assert "gs.daysAgoStart(3)" in instance.query
    assert instance.query.index("ORDERBYpriority") < instance.query.index("ORDERBYopened_at")


def test_verbose_flag_works_before_or_after_the_command():
    from sn_agent import cli

    seen = {}

    def fake_setup(settings, *, verbose=False):
        seen["verbose"] = verbose
        raise SystemExit(0)

    original = cli.setup
    cli.setup = fake_setup
    try:
        for argv, expected in ((["-v", "sweep", "--list"], True), (["sweep", "--list", "-v"], True), (["sweep", "--list"], False)):
            try:
                cli.main(argv)
            except SystemExit:
                pass
            assert seen["verbose"] is expected, argv
    finally:
        cli.setup = original


def test_log_lines_are_tagged_with_the_ticket_being_worked(caplog):
    """Several tickets run in parallel, so every line must say which ticket it
    belongs to."""

    import logging

    from sn_agent.observability import _TicketFilter, ticket_context

    logger = logging.getLogger("sn_agent.test")
    handler_filter = _TicketFilter()
    caplog.handler.addFilter(handler_filter)
    with caplog.at_level(logging.INFO, logger="sn_agent.test"):
        with ticket_context("INC0010006"):
            logger.info("inside")
        logger.info("outside")

    tags = [(r.getMessage(), r.ticket) for r in caplog.records]
    assert tags == [("inside", "INC0010006"), ("outside", "")]


def test_sweep_results_go_into_the_queue_in_priority_order():
    service = RecordingService()
    runner = JobRunner(workers=1, service_factory=lambda: service)
    low = make_ticket("INC_LOW", active=True, days_ago=0)
    low.priority = "5 - Planning"
    high = make_ticket("INC_HIGH", active=True, days_ago=0)
    high.priority = "2 - High"
    high.opened_at = NOW - timedelta(hours=1)

    queue_pending(runner, find_pending(SweepInstance([low, high]), Settings()))
    runner.start()
    runner.wait()

    assert service.order == ["INC_HIGH", "INC_LOW"]
