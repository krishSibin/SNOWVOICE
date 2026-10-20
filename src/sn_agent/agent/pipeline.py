"""The research loop.

intake -> plan queries -> retrieve (past index + live open tickets) -> grade and
bucket -> generate -> verify -> score -> gate. A low score sends it round again
with rewritten queries, looser filters and a stronger model, until the answer is
good enough or the budget runs out. It never fills a gap with a guess.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

from ..config import Settings
from ..ingest.redact import redact
from ..llm import BudgetExceeded, LLMClient, LLMUnavailable, Meter, Tier
from ..models import Candidate, Chunk, SearchScope, Ticket
from ..observability import get_tracer
from ..sources.base import KnowledgeSource
from .confidence import agreement_score, compute_confidence
from .outcome import (
    active_related,
    apply_grades,
    decide_outcome,
    is_major_incident,
    similar_without_resolution,
    split_buckets,
)
from .prompts import PROMPT_VERSION
from .safety import flag_text
from .prompts.generate import (
    EVIDENCE_BLOCK,
    GENERATE_FEEDBACK,
    GENERATE_PROMPT,
    GENERATE_SYSTEM,
)
from .prompts.diagnose import (
    DIAGNOSE_PROMPT,
    DIAGNOSE_SYSTEM,
    HISTORY_NONE,
    HISTORY_WITH_UNDOCUMENTED,
)
from .prompts.grade import CANDIDATE_BLOCK, GRADE_PROMPT, GRADE_SYSTEM
from .prompts.intake import (
    INTAKE_PROMPT,
    INTAKE_SYSTEM,
    PLAN_FEEDBACK,
    PLAN_PROMPT,
    PLAN_SYSTEM,
)
from .schemas import (
    ConfidenceReport,
    Diagnostics,
    EvidenceRef,
    GradeBatch,
    QueryPlan,
    ResolutionDraft,
    RunResult,
    TicketFacts,
)
from .verify import VerifiedDraft, verify_draft

log = logging.getLogger(__name__)

QUERIES_PER_PLAN = 4
BUCKET_A_CHAR_BUDGET = 12_000
BUCKET_C_CHAR_BUDGET = 4_000
CANDIDATE_BODY_CHARS = 2_000


class ResolutionPipeline:
    def __init__(self, settings: Settings, llm: LLMClient, source: KnowledgeSource):
        self.settings = settings
        self.llm = llm
        self.source = source

    # --- entry point --------------------------------------------------------

    def run(self, ticket: Ticket, *, as_of: datetime | None = None, include_open: bool = True) -> RunResult:
        started = time.time()
        meter = Meter(
            self.settings.prices,
            self.settings.max_cost_usd_per_ticket,
            self.settings.max_tokens_per_ticket,
        )
        result = RunResult(
            run_id=str(uuid.uuid4()),
            ticket_number=ticket.number,
            ticket_sys_id=ticket.sys_id,
            table=ticket.table,
        )
        trace: list[dict] = [{"step": "start", "prompt_version": PROMPT_VERSION}]
        tracer = get_tracer()

        candidates: dict[str, Candidate] = {}
        open_candidates: list[Candidate] = []
        graded: dict[str, Candidate] = {}
        best: tuple[ConfidenceReport, VerifiedDraft, ResolutionDraft, list[Candidate]] | None = None
        previous_queries: list[str] = []
        feedback_reasons: list[str] = []
        tier = Tier.FLASH
        iteration = 0
        facts: TicketFacts | None = None

        try:
            log.info("step 1: reading the ticket to extract the facts")
            with tracer.start_as_current_span("intake"):
                facts = self._intake(ticket, meter)
                trace.append({"step": "intake", "summary": facts.summary, "keywords": facts.keywords})
            log.info(
                "   summary: %s | error codes: %s",
                facts.summary or "-",
                ", ".join(facts.error_codes) or "none",
            )

            if include_open:
                log.info("step 2: checking for other open tickets about the same thing")
                with tracer.start_as_current_span("open_tickets"):
                    open_queries = [ticket.short_description, *facts.symptoms[:1], *facts.error_codes[:1]]
                    open_candidates = self.source.find_open(ticket, open_queries, meter)
                    trace.append({"step": "open_tickets", "found": [c.number for c in open_candidates]})
                log.info("   %s", _numbers(open_candidates, "open ticket(s)") or "none found")

            while True:
                iteration += 1
                log.info(
                    "step 3: research pass %d of %d (search scope: %s)",
                    iteration,
                    self.settings.max_iterations,
                    SCOPE_NAMES.get(iteration - 1, "anywhere"),
                )
                with tracer.start_as_current_span(f"iteration_{iteration}"):
                    queries = self._plan(facts, previous_queries, feedback_reasons, meter)
                    previous_queries.extend(queries)
                    trace.append({"step": "plan", "iteration": iteration, "queries": queries})
                    log.info("   search queries: %s", " | ".join(f'"{q}"' for q in queries))

                    found = self._retrieve(ticket, queries, iteration - 1, as_of, meter)
                    for candidate in found:
                        candidates.setdefault(candidate.number, candidate)
                    log.info("   searched ServiceNow: %s", _numbers(found, "candidate ticket(s)") or "nothing found")

                    to_grade = [
                        c for c in [*candidates.values(), *open_candidates] if c.number not in graded
                    ]
                    if to_grade:
                        log.info("   grading %d ticket(s) for relevance", len(to_grade))
                        for candidate in apply_grades(to_grade, self._grade(facts, to_grade, meter)):
                            graded[candidate.number] = candidate
                    bucket_a, bucket_b, bucket_c = split_buckets(list(graded.values()))
                    dropped = [c.number for c in to_grade if c.number not in graded]
                    log.info(
                        "   A documented fix: %s | B no write-up: %s | C open now: %s | dropped as unrelated: %s",
                        _list(bucket_a), _list(bucket_b), _list(bucket_c), ", ".join(dropped) or "none",
                    )
                    trace.append(
                        {
                            "step": "retrieve",
                            "iteration": iteration,
                            "considered": [c.number for c in to_grade],
                            "bucket_a": [c.number for c in bucket_a],
                            "bucket_b": [c.number for c in bucket_b],
                            "bucket_c": [c.number for c in bucket_c],
                        }
                    )

                    if not bucket_a:
                        # Nothing with a documented fix. One wider sweep, then stop:
                        # looping cannot conjure a write-up that was never made.
                        if iteration >= 2 or iteration >= self.settings.max_iterations:
                            log.info("   still no documented fix after a wider search; stopping without inventing one")
                            break
                        log.info("   no documented fix yet; searching wider")
                        feedback_reasons = ["no resolved ticket with a documented fix was found"]
                        continue

                    log.info("step 4: drafting the answer from the documented fixes (%s model)", tier.value)
                    draft = self._generate(ticket, facts, bucket_a, bucket_b, bucket_c, feedback_reasons, tier, meter)
                    log.info(
                        "   draft: %s, %d step(s)",
                        "root cause given" if draft.root_cause and draft.root_cause.text.strip() else "no root cause",
                        len(draft.resolution_steps),
                    )
                    log.info("step 5: checking every citation against its source")
                    evidence = _evidence_map([*bucket_a, *bucket_c])
                    fix_ids = {eid for c in bucket_a for eid in c.evidence_ids}
                    verified, verification = verify_draft(
                        draft,
                        evidence=evidence,
                        fix_evidence_ids=fix_ids,
                        open_numbers={c.number for c in bucket_c},
                        llm=self.llm,
                        meter=meter,
                    )
                    confidence = compute_confidence(
                        settings=self.settings,
                        bucket_a=bucket_a,
                        steps=verified.resolution_steps,
                        root_cause=verified.root_cause,
                        verification=verification,
                        self_confidence=draft.self_confidence,
                    )
                    confidence = self._maybe_agreement(
                        confidence, ticket, facts, bucket_a, bucket_b, bucket_c, verified, verification,
                        draft, tier, meter,
                    )
                    trace.append(
                        {
                            "step": "score",
                            "iteration": iteration,
                            "confidence": confidence.score,
                            "level": confidence.level,
                            "signals": confidence.signals,
                            "verification": verification.model_dump(),
                        }
                    )

                    removed = (
                        len(verification.removed_invalid_citation)
                        + len(verification.removed_wrong_bucket)
                        + len(verification.removed_unsupported)
                    )
                    log.info(
                        "   %d claim(s) checked, %d kept, %d removed (fake citation: %d, wrong source: %d, "
                        "not backed by the source: %d)",
                        verification.claims_generated,
                        verification.claims_kept,
                        removed,
                        len(verification.removed_invalid_citation),
                        len(verification.removed_wrong_bucket),
                        len(verification.removed_unsupported),
                    )
                    log.info(
                        "step 6: confidence %.0f/100 (%s) | %s",
                        confidence.score,
                        confidence.level,
                        ", ".join(f"{k} {v:.2f}" for k, v in confidence.signals.items()),
                    )

                    if best is None or confidence.score > best[0].score:
                        best = (confidence, verified, draft, [bucket_a, bucket_b, bucket_c])
                        result.verification = verification

                    if confidence.level != "low":
                        log.info("   good enough to send")
                        break
                    if iteration >= self.settings.max_iterations or meter.exhausted():
                        log.info("   still low, and out of research passes or budget; reporting what was found")
                        break
                    feedback_reasons = _failure_reasons(confidence, verification)
                    tier = Tier.PRO
                    log.info("   too low: %s. Trying again with the stronger model", "; ".join(feedback_reasons))
        except BudgetExceeded as exc:
            log.warning("stopped early, per-ticket budget spent: %s", exc)
            result.budget_exhausted = True
            result.stopped_early = "per-ticket budget spent"
            trace.append({"step": "budget_exhausted", "detail": str(exc)})
        except LLMUnavailable as exc:
            # An overloaded model must not cost us the whole run: what we already
            # found — duplicates especially — is still worth posting.
            log.error("stopped early, no model could answer: %s", exc)
            result.stopped_early = "the language model was unavailable"
            trace.append({"step": "llm_unavailable", "detail": str(exc)})

        bucket_a, bucket_b, bucket_c = (
            best[3] if best else split_buckets(list(graded.values()))
        )
        diagnostics = None
        if best is None and facts is not None and not result.stopped_early:
            # No documented fix anywhere. The note should still give the engineer
            # somewhere to start, so ask for checks — never for a cause.
            log.info("no documented fix: asking for diagnostic questions instead")
            try:
                diagnostics = self._diagnose(facts, bucket_b, meter)
                trace.append({"step": "diagnose", "questions": diagnostics.diagnostic_questions})
            except (BudgetExceeded, LLMUnavailable) as exc:
                log.warning("diagnostic questions skipped: %s", exc)

        self._finalize(result, best, bucket_a, bucket_b, bucket_c, diagnostics)
        result.iterations = iteration
        result.cost_usd = round(meter.cost_usd, 6)
        result.tokens = meter.tokens
        result.models_used = meter.models_used
        result.latency_s = round(time.time() - started, 2)
        result.trace = trace + [{"step": "llm_calls", "calls": meter.calls}]
        log.info(
            "step 7: outcome %s | %d fix step(s) | %d open related | %d model calls | $%.4f | %.0fs",
            result.outcome.value,
            len(result.resolution_steps),
            len(result.active_related),
            len(meter.calls),
            result.cost_usd,
            result.latency_s,
        )
        return result

    # --- steps --------------------------------------------------------------

    def _intake(self, ticket: Ticket, meter: Meter) -> TicketFacts:
        return self.llm.generate_json(
            tier=Tier.LITE,
            system=INTAKE_SYSTEM,
            prompt=INTAKE_PROMPT.format(
                number=ticket.number,
                category=ticket.category or "-",
                ci=ticket.cmdb_ci or ticket.business_service or "-",
                assignment_group=ticket.assignment_group or "-",
                short_description=redact(ticket.short_description),
                description=redact(ticket.description)[:4000],
            ),
            schema=TicketFacts,
            meter=meter,
            step="intake",
        )

    def _plan(
        self, facts: TicketFacts, previous: list[str], reasons: list[str], meter: Meter
    ) -> list[str]:
        feedback = ""
        if reasons:
            feedback = PLAN_FEEDBACK.format(
                previous="; ".join(previous[-8:]) or "-", reasons="; ".join(reasons)
            )
        plan = self.llm.generate_json(
            tier=Tier.LITE,
            system=PLAN_SYSTEM.replace("{n}", str(QUERIES_PER_PLAN)),
            prompt=PLAN_PROMPT.format(
                summary=facts.summary,
                symptoms="; ".join(facts.symptoms) or "-",
                error_codes="; ".join(facts.error_codes) or "-",
                component=facts.affected_component or "-",
                keywords="; ".join(facts.keywords) or "-",
                feedback=feedback,
            ),
            schema=QueryPlan,
            meter=meter,
            step="plan",
        )
        queries = [q.strip() for q in plan.queries if q.strip()][:QUERIES_PER_PLAN]
        return queries or [facts.summary or " ".join(facts.keywords)]

    def _retrieve(
        self, ticket: Ticket, queries: list[str], loosen_level: int, as_of: datetime | None, meter: Meter
    ) -> list[Candidate]:
        scope = SearchScope(
            level=loosen_level,
            recency_days=0 if loosen_level >= 2 else self.settings.search_recency_days,
            as_of=as_of,
        )
        return self.source.search(
            ticket=ticket,
            queries=queries,
            scope=scope,
            limit=self.settings.max_candidate_tickets,
            meter=meter,
        )

    def _grade(self, facts: TicketFacts, candidates: list[Candidate], meter: Meter) -> list:
        blocks = []
        for candidate in candidates:
            body = "\n".join(
                f"[{chunk.evidence_id}] {chunk.field}: {chunk.text}" for chunk in candidate.chunks
            )[:CANDIDATE_BODY_CHARS]
            extra = ""
            if candidate.is_open:
                extra = f" | Opened: {_date(candidate.opened_at)} | State: {candidate.state_label or 'open'}"
            elif candidate.resolved_at:
                extra = f" | Resolved: {_date(candidate.resolved_at)}"
            blocks.append(
                CANDIDATE_BLOCK.format(
                    number=candidate.number,
                    status="OPEN" if candidate.is_open else "RESOLVED",
                    table=candidate.table,
                    category=candidate.category or "-",
                    ci=candidate.cmdb_ci or "-",
                    group=candidate.assignment_group or "-",
                    extra=extra,
                    body=body,
                )
            )
        batch = self.llm.generate_json(
            tier=Tier.LITE,
            system=GRADE_SYSTEM,
            prompt=GRADE_PROMPT.format(
                summary=facts.summary,
                symptoms="; ".join(facts.symptoms) or "-",
                error_codes="; ".join(facts.error_codes) or "-",
                component=facts.affected_component or "-",
                candidates="\n\n".join(blocks),
            ),
            schema=GradeBatch,
            meter=meter,
            step="grade",
        )
        return batch.grades

    def _generate(
        self,
        ticket: Ticket,
        facts: TicketFacts,
        bucket_a: list[Candidate],
        bucket_b: list[Candidate],
        bucket_c: list[Candidate],
        reasons: list[str],
        tier: Tier,
        meter: Meter,
    ) -> ResolutionDraft:
        feedback = GENERATE_FEEDBACK.format(reasons="\n".join(f"- {r}" for r in reasons)) if reasons else ""
        prompt = GENERATE_PROMPT.format(
            number=ticket.number,
            summary=facts.summary,
            symptoms="; ".join(facts.symptoms) or "-",
            error_codes="; ".join(facts.error_codes) or "-",
            component=facts.affected_component or "-",
            description=redact(ticket.description)[:3000],
            bucket_a=_render_evidence(bucket_a, BUCKET_A_CHAR_BUDGET) or "(none)",
            bucket_b=_render_bucket_b(bucket_b) or "(none)",
            bucket_c=_render_evidence(bucket_c, BUCKET_C_CHAR_BUDGET, open_tickets=True) or "(none)",
            feedback=feedback,
        )
        return self.llm.generate_json(
            tier=tier,
            system=GENERATE_SYSTEM,
            prompt=prompt,
            schema=ResolutionDraft,
            meter=meter,
            step=f"generate_{tier.value}",
        )

    def _diagnose(self, facts: TicketFacts, bucket_b: list[Candidate], meter: Meter) -> Diagnostics:
        if bucket_b:
            tickets = "\n".join(
                f"- {c.number}, closed by {c.resolved_by or 'unknown'} ({c.assignment_group or 'unknown group'})"
                for c in bucket_b[:5]
            )
            history = HISTORY_WITH_UNDOCUMENTED.format(tickets=tickets)
        else:
            history = HISTORY_NONE
        return self.llm.generate_json(
            tier=Tier.LITE,
            system=DIAGNOSE_SYSTEM,
            prompt=DIAGNOSE_PROMPT.format(
                summary=facts.summary,
                symptoms="; ".join(facts.symptoms) or "-",
                error_codes="; ".join(facts.error_codes) or "-",
                component=facts.affected_component or "-",
                history=history,
            ),
            schema=Diagnostics,
            meter=meter,
            step="diagnose",
        )

    def _maybe_agreement(
        self, confidence, ticket, facts, bucket_a, bucket_b, bucket_c, verified, verification, draft,
        tier, meter,
    ) -> ConfidenceReport:
        """Only for borderline scores: re-generate a couple of times and see
        whether the answer keeps landing on the same past tickets."""

        band = self.settings.agreement_band
        if self.settings.agreement_samples <= 0:
            return confidence
        if not (self.settings.confidence_high - band <= confidence.score < self.settings.confidence_high):
            return confidence

        samples = [{eid.split(":", 1)[0] for c in verified.resolution_steps for eid in c.evidence_ids}]
        for _ in range(self.settings.agreement_samples):
            if meter.exhausted():
                break
            extra = self._generate(ticket, facts, bucket_a, bucket_b, bucket_c, [], tier, meter)
            samples.append(
                {eid.split(":", 1)[0] for step in extra.resolution_steps for eid in step.evidence_ids}
            )
        return compute_confidence(
            settings=self.settings,
            bucket_a=bucket_a,
            steps=verified.resolution_steps,
            root_cause=verified.root_cause,
            verification=verification,
            self_confidence=draft.self_confidence,
            agreement=agreement_score(samples),
        )

    # --- assembling the result ---------------------------------------------

    def _finalize(self, result: RunResult, best, bucket_a, bucket_b, bucket_c, diagnostics=None) -> None:
        if best is not None:
            confidence, verified, draft, _ = best
            result.confidence = confidence
            result.root_cause = verified.root_cause
            result.resolution_steps = list(verified.resolution_steps)
            result.other_claims = list(verified.other_claims)
            result.gaps = list(draft.gaps)
            result.diagnostic_questions = list(draft.diagnostic_questions)
            active_drafts = verified.active_related
        else:
            active_drafts = {}
            if diagnostics is not None:
                result.gaps = list(diagnostics.gaps)
                result.diagnostic_questions = list(diagnostics.diagnostic_questions)

        result.similar_without_resolution = similar_without_resolution(bucket_b)
        result.active_related = active_related(bucket_c, active_drafts)
        result.active_issue_in_progress = any(
            entry.relation == "duplicate" for entry in result.active_related
        ) or any(candidate.grade_score >= 0.6 for candidate in bucket_c)
        result.major_incident_warning = is_major_incident(bucket_c, self.settings)
        result.outcome = decide_outcome(
            has_steps=bool(result.resolution_steps),
            level=result.confidence.level,
            bucket_a=bucket_a,
            bucket_b=bucket_b,
        )
        result.evidence = _cited_evidence(result, [*bucket_a, *bucket_c])
        self._flag_risky_steps(result)

    def _flag_risky_steps(self, result: RunResult) -> None:
        instance_host = urlparse(self.settings.servicenow_instance_url).hostname or ""
        claims = [*result.resolution_steps, *result.other_claims]
        if result.root_cause:
            claims.append(result.root_cause)
        for claim in claims:
            claim.flags = flag_text(claim.text, trusted_hosts=(instance_host,))
        flagged = [c for c in claims if c.flags]
        if flagged:
            kinds = sorted({flag for c in flagged for flag in c.flags})
            log.warning("%d suggested step(s) flagged for a human to check: %s", len(flagged), ", ".join(kinds))


# --- helpers ----------------------------------------------------------------


SCOPE_NAMES = {0: "same system and category", 1: "same category"}


def _list(candidates: list[Candidate]) -> str:
    return ", ".join(c.number for c in candidates) or "none"


def _numbers(candidates: list[Candidate], noun: str) -> str:
    if not candidates:
        return ""
    return f"{len(candidates)} {noun}: {_list(candidates)}"


def _render_evidence(candidates: list[Candidate], char_budget: int, *, open_tickets: bool = False) -> str:
    blocks: list[str] = []
    used = 0
    for candidate in candidates:
        for chunk in candidate.chunks:
            meta = f" / opened {_date(candidate.opened_at)}" if open_tickets else (
                f" / resolved {_date(candidate.resolved_at)}" if candidate.resolved_at else ""
            )
            block = EVIDENCE_BLOCK.format(
                evidence_id=chunk.evidence_id,
                number=chunk.number,
                field=chunk.field,
                meta=meta,
                text=chunk.text,
            )
            if used + len(block) > char_budget:
                continue
            used += len(block)
            blocks.append(block)
    return "\n\n".join(blocks)


def _render_bucket_b(candidates: list[Candidate]) -> str:
    lines = []
    for candidate in candidates:
        description = next((c.text for c in candidate.chunks if c.field == "description"), "")
        lines.append(
            f"- {candidate.number} ({_date(candidate.resolved_at)}, resolved by "
            f"{candidate.resolved_by or 'unknown'}, group {candidate.assignment_group or 'unknown'}): "
            f"{description[:200]} — NO RESOLUTION RECORDED"
        )
    return "\n".join(lines)


def _evidence_map(candidates: list[Candidate]) -> dict[str, Chunk]:
    return {chunk.evidence_id: chunk for candidate in candidates for chunk in candidate.chunks}


def _cited_evidence(result: RunResult, candidates: list[Candidate]) -> list[EvidenceRef]:
    evidence = _evidence_map(candidates)
    claims = [*result.resolution_steps, *result.other_claims]
    if result.root_cause:
        claims.append(result.root_cause)
    seen: dict[str, EvidenceRef] = {}
    for claim in claims:
        for evidence_id in claim.evidence_ids:
            chunk = evidence.get(evidence_id)
            if chunk is None or evidence_id in seen:
                continue
            seen[evidence_id] = EvidenceRef(
                evidence_id=evidence_id,
                number=chunk.number,
                table=chunk.table,
                sys_id=chunk.sys_id,
                field=chunk.field,
                snippet=chunk.text[:300],
            )
    return list(seen.values())


def _failure_reasons(confidence: ConfidenceReport, verification) -> list[str]:
    reasons: list[str] = []
    if verification.removed_unsupported:
        reasons.append(
            f"{len(verification.removed_unsupported)} claim(s) were not supported by the evidence cited"
        )
    if verification.removed_invalid_citation:
        reasons.append(f"{len(verification.removed_invalid_citation)} claim(s) cited evidence that does not exist")
    if verification.removed_wrong_bucket:
        reasons.append(
            f"{len(verification.removed_wrong_bucket)} claim(s) cited a ticket with no documented resolution"
        )
    if confidence.signals.get("corroboration", 0) < 0.4:
        reasons.append("the answer rests on too few past tickets; look for more")
    if confidence.signals.get("retrieval_strength", 0) < 0.5:
        reasons.append("no strongly matching past ticket was found; try different search wording")
    return reasons or ["confidence too low to send"]


def _date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d") if value else "unknown"
