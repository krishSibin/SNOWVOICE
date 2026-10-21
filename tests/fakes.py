"""Test doubles: a scripted LLM and a fake ServiceNow instance, so the pipeline
can be exercised end to end without network access."""

from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel

from sn_agent.agent.schemas import ClaimSupport, GradeBatch, SupportBatch, TicketGrade
from sn_agent.llm import Meter, Tier
from sn_agent.servicenow.client import ZING_OPERATOR


class FakeLLM:
    def __init__(self, handlers: dict[str, Callable[[str], BaseModel]] | None = None):
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str, str]] = []

    def generate_json(self, *, tier: Tier, system: str, prompt: str, schema, meter: Meter, step: str = ""):
        meter.ensure_available(step)
        self.calls.append((step, tier.value, prompt))
        meter.record(f"fake-{tier.value}", step, max(1, len(prompt) // 4), 120)
        handler = self.handlers.get(step) or self.handlers.get(schema.__name__)
        if handler is None:
            return schema()
        return handler(prompt)

    def steps(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeServiceNow:
    """Holds tickets in memory and answers the client's query shapes: full-text
    search over the ticket text, field equality for the scoped sweeps, and the
    journal lookup."""

    def __init__(self, tickets: list | None = None):
        self.tickets = list(tickets or [])
        self.base_url = "https://example.service-now.com"
        self.posted: list[tuple[str, str, str]] = []
        self.queries: list[tuple[str, str]] = []

    # --- reads --------------------------------------------------------------

    def search_text(self, table: str, text: str, *, clauses=None, limit: int = 20):
        self.queries.append((table, f"{ZING_OPERATOR}={text}"))
        terms = {t for t in re.findall(r"[a-z0-9-]+", text.lower()) if len(t) > 2}
        matches = []
        for ticket in self._eligible(table, clauses or []):
            haystack = " ".join(
                [ticket.short_description, ticket.description, ticket.resolution_text,
                 *(j.value for j in ticket.journal)]
            ).lower()
            hits = sum(1 for term in terms if term in haystack)
            if hits:
                matches.append((hits, ticket))
        matches.sort(key=lambda pair: pair[0], reverse=True)
        return [self._record(t) for _hits, t in matches[:limit]]

    def search_by_fields(self, table: str, equals: dict, *, clauses=None, limit: int = 20, order_by: str = ""):
        self.queries.append((table, str(equals)))
        matches = []
        for ticket in self._eligible(table, clauses or []):
            values = {
                "cmdb_ci": ticket.cmdb_ci_id or ticket.cmdb_ci,
                "business_service": ticket.business_service,
                "category": ticket.category,
            }
            if all(values.get(field) == value for field, value in equals.items()):
                matches.append(ticket)
        return [self._record(t) for t in matches[:limit]]

    def query_records(self, table: str, query: str, **_kw):
        """Every ticket on the table, as the index sync reads them."""

        self.queries.append((table, query))
        return [self._record(t) for t in self.tickets if t.table == table]

    def get_journal(self, table, sys_ids, elements=("work_notes", "comments")):
        wanted = set(sys_ids)
        return {t.sys_id: t.journal for t in self.tickets if t.sys_id in wanted}

    def get_ticket(self, number: str, table: str = "incident", *, with_journal: bool = True):
        return next(t for t in self.tickets if t.number == number)

    def to_ticket(self, record, table):
        return record["_ticket"]

    # --- write --------------------------------------------------------------

    def post_work_note(self, table: str, sys_id: str, note: str):
        self.posted.append((table, sys_id, note))
        ticket = next((t for t in self.tickets if t.sys_id == sys_id), None)
        if ticket is not None:
            from sn_agent.models import JournalEntry

            ticket.journal.append(JournalEntry(element="work_notes", value=note))
        return {"sys_id": sys_id}

    # --- helpers ------------------------------------------------------------

    def _eligible(self, table: str, clauses: list[str]):
        excluded = {c.split("!=")[1] for c in clauses if c.startswith("number!=")}
        wants_open = "active=true" in clauses
        wants_resolved = "resolved_atISNOTEMPTY" in clauses
        out = []
        for ticket in self.tickets:
            if ticket.table != table or ticket.number in excluded:
                continue
            if wants_open and not ticket.active:
                continue
            if wants_resolved and ticket.active:
                continue
            out.append(ticket)
        return out

    @staticmethod
    def _record(ticket):
        return {"_ticket": ticket, "number": ticket.number, "sys_id": ticket.sys_id}


class LevelVerdict(BaseModel):
    """Shape of the resolution-level classifier's reply."""

    level: str = "weak"
    reason: str = "test"


def level_handler(prompt: str) -> LevelVerdict:
    """Close notes that describe an action are rich; anything else is weak."""

    actions = ("re-issued", "restarted", "replaced", "cleared", "upgraded", "lowered", "rebuilt")
    return LevelVerdict(level="rich" if any(a in prompt.lower() for a in actions) else "weak")


def grade_handler(
    mapping: dict[str, tuple[str, float, str]], default: tuple[str, float, str] = ("irrelevant", 0.0, "unrelated")
) -> Callable[[str], GradeBatch]:
    def handle(prompt: str) -> GradeBatch:
        numbers = re.findall(r'<candidate number="([^"]+)"', prompt)
        grades = []
        for number in numbers:
            relevance, score, relation = mapping.get(number, default)
            grades.append(
                TicketGrade(number=number, relevance=relevance, score=score, relation=relation, reason="test")
            )
        return GradeBatch(grades=grades)

    return handle


def support_handler(
    default: str = "supported", overrides: dict[str, str] | None = None
) -> Callable[[str], SupportBatch]:
    overrides = overrides or {}

    def handle(prompt: str) -> SupportBatch:
        claim_ids = re.findall(r"claim_id: (\S+)", prompt)
        return SupportBatch(
            results=[
                ClaimSupport(claim_id=cid, label=overrides.get(cid, default), reason="test")
                for cid in claim_ids
            ]
        )

    return handle


def evidence_ids_in(prompt: str, section: str) -> list[str]:
    """Pull the evidence ids the generator was shown in one section, so a fake
    draft can cite something that really exists."""

    start = prompt.find(section)
    if start == -1:
        return []
    end = prompt.find("\n===", start + len(section))
    body = prompt[start : end if end != -1 else len(prompt)]
    return re.findall(r"\[([A-Z]+\d+:[a-z_]+:\d+)\]", body)
