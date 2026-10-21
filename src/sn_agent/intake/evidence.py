"""Finds documented fixes for an issue described in conversation.

Reuses the ticket agent's search, bucketing and citation checks, with three
differences that matter when someone is waiting on the line:

- one research pass, no loop and no agreement sampling;
- ServiceNow queries and resolution-level checks run in parallel;
- the agent's own earlier work notes are dropped before anything else sees
  them, so a past suggestion is never mistaken for a confirmed fix.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..agent.outcome import apply_grades, is_major_incident, split_buckets
from ..agent.prompts.generate import EVIDENCE_BLOCK
from ..agent.prompts.grade import CANDIDATE_BLOCK, GRADE_PROMPT, GRADE_SYSTEM
from ..agent.safety import flag_text
from ..agent.schemas import CitedClaim, GradeBatch, ResolutionDraft
from ..agent.verify import verify_draft
from ..config import Settings
from ..ingest.chunking import chunk_ticket
from ..ingest.redact import redact
from ..ingest.resolution_level import classify_resolution_level
from ..llm import LLMClient, Meter, Tier
from ..models import Candidate, ResolutionLevel, SearchScope, Ticket
from ..notify.formatting import HEADER as AGENT_NOTE_HEADER
from ..sources.servicenow_search import HISTORY_TABLES, RRF_K, ServiceNowSearchSource
from .prompts import FIX_PROMPT, FIX_SYSTEM

log = logging.getLogger(__name__)

CANDIDATE_BODY_CHARS = 1_500
FIX_EVIDENCE_CHARS = 9_000
# Stands in for a ticket number on an issue that has none yet.
CONVERSATION_NUMBER = "INTAKE"


class HumanNotesOnly:
    """Wraps a ServiceNow client so journal reads leave out the ticket agent's
    own notes. Without this, a past ticket where the agent *suggested* a fix
    would come back as a ticket whose notes *describe* that fix, and be cited
    as if it had been confirmed."""

    def __init__(self, client: Any, agent_authors: list[str] | None = None):
        self._client = client
        self._authors = {a.strip().lower() for a in agent_authors or [] if a.strip()}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def get_journal(self, table, sys_ids, elements=("work_notes", "comments")):
        journals = self._client.get_journal(table, sys_ids, elements)
        return {sys_id: [e for e in entries if self.is_human(e)] for sys_id, entries in journals.items()}

    def is_human(self, entry) -> bool:
        if AGENT_NOTE_HEADER in (entry.value or ""):
            return False
        return (entry.created_by or "").strip().lower() not in self._authors


class ParallelSearchSource(ServiceNowSearchSource):
    """The ticket agent's search, with its independent ServiceNow queries and
    resolution-level checks run side by side instead of one after another."""

    def __init__(self, client, settings: Settings, llm: LLMClient, *, threads: int = 4):
        super().__init__(client, settings, llm)
        self.threads = max(1, threads)

    def search(
        self, *, ticket: Ticket, queries: list[str], scope: SearchScope, limit: int, meter: Meter
    ) -> list[Candidate]:
        jobs: list[tuple[str, str, Any]] = []
        for table in HISTORY_TABLES:
            clauses = self._history_clauses(table, ticket, scope)
            for query in queries:
                jobs.append((table, "text", (query, clauses)))
            jobs.append((table, "sweep", clauses))

        def run(job):
            table, route, arg = job
            if route == "text":
                query, clauses = arg
                rows = self.client.search_text(
                    table, query, clauses=clauses, limit=self.settings.search_results_per_query
                )
                return [[(table, row, "text") for row in rows]]
            return [[(table, row, "sweep") for row in rows] for rows in self._sweeps(table, ticket, scope, arg)]

        ranked: list[list[tuple[str, dict[str, Any], str]]] = []
        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            for lists in pool.map(run, jobs):
                ranked.extend(lists)
        return self._to_candidates(ranked, limit, meter)

    def _to_candidates(self, ranked, limit: int, meter: Meter) -> list[Candidate]:
        scores: dict[tuple[str, str], float] = defaultdict(float)
        records: dict[tuple[str, str], dict[str, Any]] = {}
        for results in ranked:
            for rank, (table, row, route) in enumerate(results):
                ticket = self.client.to_ticket(row, table)
                if not ticket.number:
                    continue
                key = (table, ticket.number)
                scores[key] += (1.0 if route == "text" else 0.6) / (RRF_K + rank + 1)
                records.setdefault(key, row)

        top = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        if not top:
            return []
        tickets = [(self.client.to_ticket(records[key], key[0]), score) for key, score in top]
        by_table: dict[str, list[str]] = defaultdict(list)
        for ticket, _score in tickets:
            by_table[ticket.table].append(ticket.sys_id)
        journals: dict[str, list] = {}
        for table, sys_ids in by_table.items():
            journals.update(self.client.get_journal(table, sys_ids))

        def build(pair: tuple[Ticket, float]) -> Candidate | None:
            ticket, score = pair
            ticket.journal = journals.get(ticket.sys_id, [])
            level = (
                ResolutionLevel.NONE
                if ticket.active
                else classify_resolution_level(ticket, llm=self.llm, meter=meter)
            )
            chunks = chunk_ticket(ticket, level, max_chars=self.settings.chunk_max_chars)
            return Candidate.from_chunks(chunks, score) if chunks else None

        with ThreadPoolExecutor(max_workers=self.threads) as pool:
            return [c for c in pool.map(build, tickets) if c is not None]


# --- what the conversation passes in and gets back ------------------------


@dataclass
class IssueReport:
    """What the conversation has captured so far."""

    summary: str
    system: str = ""
    error_text: str = ""
    details: str = ""
    category: str = ""

    def as_ticket(self) -> Ticket:
        """The search code works on tickets; a not-yet-raised issue is shaped
        like one. It has no number, so nothing is excluded from the results."""

        return Ticket(
            sys_id="",
            number=CONVERSATION_NUMBER,
            short_description=self.summary,
            description="\n".join(p for p in (self.details, self.error_text) if p),
            category=self.category,
        )


class UserStep(BaseModel):
    text: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    needs_technician: bool = False


class UserFixDraft(BaseModel):
    cause: str = ""
    steps: list[UserStep] = Field(default_factory=list)


@dataclass
class FixStep:
    text: str
    tickets: list[str]
    needs_technician: bool


@dataclass
class FixSearch:
    # fix_found: steps the user can do | fix_needs_technician: fixed before, but
    # only IT can do it | no_documented_fix | no_history
    status: str
    steps: list[FixStep] = field(default_factory=list)
    cause: str = ""
    fixed_before: list[str] = field(default_factory=list)
    similar_without_fix: list[str] = field(default_factory=list)
    open_now: list[str] = field(default_factory=list)
    major_incident: bool = False
    # Every graded candidate, for choosing the assignment group later.
    group_votes: dict[str, float] = field(default_factory=dict)
    category_votes: dict[str, float] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    error: str = ""

    def for_model(self) -> dict[str, Any]:
        """What the conversation model sees: steps it may read out, and nothing
        it could mistake for a fix."""

        user_steps = [s for s in self.steps if not s.needs_technician]
        out: dict[str, Any] = {"status": self.status}
        if self.status == "fix_found":
            out["steps_for_user"] = [{"step": s.text, "from_tickets": s.tickets} for s in user_steps]
            out["steps_needing_technician"] = sum(1 for s in self.steps if s.needs_technician)
            if self.cause:
                out["cause"] = self.cause
        elif self.status == "fix_needs_technician":
            out["fixed_before_in"] = self.fixed_before[:3]
            if self.cause:
                out["cause"] = self.cause
            out["note"] = (
                "This was fixed before, but by the IT team: the fix needs admin or server access, so give "
                "the user no steps. Tell them it has been seen and fixed before (say the cause if given), "
                "and offer to raise a ticket so IT can apply the same fix."
            )
        elif self.status == "no_documented_fix":
            out["note"] = (
                "The same issue happened before but nobody recorded how it was fixed. "
                "Do not suggest a fix; offer to raise a ticket."
            )
            out["similar_tickets"] = self.similar_without_fix[:3]
        else:
            out["note"] = "No similar past ticket. Do not suggest a fix; offer to raise a ticket."
        if self.open_now:
            out["open_tickets_for_same_issue"] = len(self.open_now)
            out["note_open"] = "Others are reporting this right now; tell the user IT is already aware."
        if self.major_incident:
            out["possible_major_incident"] = True
        if self.error:
            out["note"] = "Search failed; apologise and offer to raise a ticket."
        return out


class FixFinder:
    def __init__(self, settings: Settings, llm: LLMClient, source: ParallelSearchSource):
        self.settings = settings
        self.llm = llm
        self.source = source

    def find(self, issue: IssueReport) -> FixSearch:
        meter = Meter(self.settings.prices, self.settings.max_cost_usd_per_ticket, self.settings.max_tokens_per_ticket)
        timings: dict[str, float] = {}
        started = time.time()
        ticket = issue.as_ticket()
        queries = _queries(issue)

        with ThreadPoolExecutor(max_workers=2) as pool:
            past_job = pool.submit(
                self.source.search,
                ticket=ticket,
                queries=queries,
                scope=SearchScope(level=1, recency_days=self.settings.search_recency_days),
                limit=self.settings.intake_max_candidates,
                meter=meter,
            )
            open_job = pool.submit(self.source.find_open, ticket, queries[:2], meter)
            past, open_now = past_job.result(), open_job.result()
        timings["search_s"] = round(time.time() - started, 2)
        log.info(
            "intake search: %d past, %d open candidate(s) in %.1fs",
            len(past), len(open_now), timings["search_s"],
        )

        result = FixSearch(status="no_history", queries=queries, timings=timings)
        candidates = [*past, *open_now]
        if not candidates:
            return result

        mark = time.time()
        graded = apply_grades(candidates, self._grade(issue, candidates, meter))
        timings["grade_s"] = round(time.time() - mark, 2)
        bucket_a, bucket_b, bucket_c = split_buckets(graded)
        log.info(
            "intake buckets: A %s | B %s | C %s",
            [c.number for c in bucket_a], [c.number for c in bucket_b], [c.number for c in bucket_c],
        )

        for candidate in [*bucket_a, *bucket_b, *bucket_c]:
            weight = candidate.grade_score * (1.0 if candidate.bucket and candidate.bucket.value != "C" else 0.5)
            if candidate.assignment_group:
                result.group_votes[candidate.assignment_group] = (
                    result.group_votes.get(candidate.assignment_group, 0.0) + weight
                )
            if candidate.category:
                result.category_votes[candidate.category] = result.category_votes.get(candidate.category, 0.0) + weight
        result.open_now = [c.number for c in bucket_c]
        result.major_incident = is_major_incident(bucket_c, self.settings)
        result.similar_without_fix = [c.number for c in bucket_b]

        if not bucket_a:
            result.status = "no_documented_fix" if bucket_b else "no_history"
            return result

        mark = time.time()
        draft = self._draft(issue, bucket_a, meter)
        steps = self._verify(draft, bucket_a, meter)
        timings["draft_s"] = round(time.time() - mark, 2)
        if any(not step.needs_technician for step in steps):
            result.status = "fix_found"
            result.steps = steps
            result.cause = draft.cause.strip()
        elif steps or not draft.steps:
            # The tickets carry a documented fix, but nothing in it is for the
            # user: a server, a certificate, admin rights. That is not "nobody
            # wrote it down"; the IT team knows the fix, so the ticket says so.
            result.status = "fix_needs_technician"
            result.steps = steps
            result.cause = draft.cause.strip()
            result.fixed_before = [c.number for c in bucket_a]
        else:
            # Steps were drafted but none survived the citation check: what the
            # fix was is not certain, so nothing is claimed about it.
            result.status = "no_documented_fix"
            result.similar_without_fix = [c.number for c in bucket_a] + result.similar_without_fix
        timings["total_s"] = round(time.time() - started, 2)
        return result

    def _grade(self, issue: IssueReport, candidates: list[Candidate], meter: Meter):
        blocks = []
        for candidate in candidates:
            body = "\n".join(f"[{c.evidence_id}] {c.field}: {c.text}" for c in candidate.chunks)
            blocks.append(
                CANDIDATE_BLOCK.format(
                    number=candidate.number,
                    status="OPEN" if candidate.is_open else "RESOLVED",
                    table=candidate.table,
                    category=candidate.category or "-",
                    ci=candidate.cmdb_ci or "-",
                    group=candidate.assignment_group or "-",
                    extra="",
                    body=body[:CANDIDATE_BODY_CHARS],
                )
            )
        batch = self.llm.generate_json(
            tier=Tier.LITE,
            system=GRADE_SYSTEM,
            prompt=GRADE_PROMPT.format(
                summary=redact(issue.summary),
                symptoms=redact(issue.details) or "-",
                error_codes=redact(issue.error_text) or "-",
                component=issue.system or "-",
                candidates="\n\n".join(blocks),
            ),
            schema=GradeBatch,
            meter=meter,
            step="intake_grade",
        )
        return batch.grades

    def _draft(self, issue: IssueReport, bucket_a: list[Candidate], meter: Meter) -> UserFixDraft:
        blocks: list[str] = []
        used = 0
        for candidate in bucket_a:
            for chunk in candidate.chunks:
                block = EVIDENCE_BLOCK.format(
                    evidence_id=chunk.evidence_id, number=chunk.number, field=chunk.field, meta="", text=chunk.text
                )
                if used + len(block) > FIX_EVIDENCE_CHARS:
                    continue
                used += len(block)
                blocks.append(block)
        return self.llm.generate_json(
            tier=Tier.FLASH,
            system=FIX_SYSTEM,
            prompt=FIX_PROMPT.format(
                summary=redact(issue.summary),
                system=issue.system or "-",
                error_text=redact(issue.error_text) or "-",
                details=redact(issue.details)[:2000] or "-",
                bucket_a="\n\n".join(blocks),
            ),
            schema=UserFixDraft,
            meter=meter,
            step="intake_fix",
        )

    def _verify(self, draft: UserFixDraft, bucket_a: list[Candidate], meter: Meter) -> list[FixStep]:
        """The ticket agent's own citation check: every step must cite a
        resolved ticket's write-up, and a separate model call confirms the
        cited text really says it. Anything failing is dropped, not reworded."""

        evidence = {chunk.evidence_id: chunk for c in bucket_a for chunk in c.chunks}
        resolution = ResolutionDraft(
            resolution_steps=[CitedClaim(text=s.text, evidence_ids=s.evidence_ids) for s in draft.steps]
        )
        verified, report = verify_draft(
            resolution, evidence=evidence, fix_evidence_ids=set(evidence), open_numbers=set(), llm=self.llm, meter=meter
        )
        technician = {s.text.strip(): s.needs_technician for s in draft.steps}
        if report.claims_generated != report.claims_kept:
            log.info(
                "intake: %d of %d drafted step(s) dropped by the citation check",
                report.claims_generated - report.claims_kept, report.claims_generated,
            )
        steps: list[FixStep] = []
        for claim in verified.resolution_steps:
            tickets = sorted({evidence[eid].number for eid in claim.evidence_ids if eid in evidence})
            # A step with a command or a link is for the IT team, whatever the model said.
            risky = bool(flag_text(claim.text)) or bool(claim.flags)
            steps.append(FixStep(text=claim.text, tickets=tickets, needs_technician=technician.get(claim.text, False) or risky))
        return steps


def _queries(issue: IssueReport) -> list[str]:
    """No planning call: the conversation already wrote a summary, and the
    exact error text is the best full-text query there is."""

    candidates = [
        issue.error_text,
        issue.summary,
        f"{issue.system} {issue.summary}" if issue.system and issue.system.lower() not in issue.summary.lower() else "",
    ]
    out: list[str] = []
    for query in candidates:
        query = " ".join(redact(query or "").split())[:200]
        if query and query.lower() not in {q.lower() for q in out}:
            out.append(query)
    return out[:3] or [issue.summary]
