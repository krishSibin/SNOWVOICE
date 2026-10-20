"""Bucketing and outcome rules.

Deliberately not left to the LLM: which evidence may support a fix, what the
outcome is, and whether this looks like a major incident are decided in code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..models import Bucket, Candidate, ResolutionLevel
from .schemas import ActiveRelated, Outcome, SimilarNoResolution, TicketGrade

MIN_GRADE_SCORE = 0.4


def apply_grades(candidates: list[Candidate], grades: list[TicketGrade]) -> list[Candidate]:
    by_number = {g.number: g for g in grades}
    kept: list[Candidate] = []
    for candidate in candidates:
        grade = by_number.get(candidate.number)
        if grade is None:
            continue
        candidate.relevance = grade.relevance
        candidate.grade_score = max(0.0, min(1.0, grade.score))
        candidate.relation = grade.relation
        bucket = _bucket_for(candidate)
        if bucket is None:
            continue
        candidate.bucket = bucket
        kept.append(candidate)
    return kept


def _bucket_for(candidate: Candidate) -> Bucket | None:
    if candidate.relevance not in {"relevant", "partial"} or candidate.grade_score < MIN_GRADE_SCORE:
        return None
    if candidate.is_open:
        if candidate.relation not in {"duplicate", "possibly_related"}:
            return None
        return Bucket.C_ACTIVE
    if candidate.resolution_level == ResolutionLevel.RICH:
        return Bucket.A_RESOLVED_WITH_INFO
    return Bucket.B_RESOLVED_NO_INFO


def split_buckets(candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate], list[Candidate]]:
    a = [c for c in candidates if c.bucket == Bucket.A_RESOLVED_WITH_INFO]
    b = [c for c in candidates if c.bucket == Bucket.B_RESOLVED_NO_INFO]
    c = [c for c in candidates if c.bucket == Bucket.C_ACTIVE]
    for group in (a, b, c):
        group.sort(key=lambda x: x.grade_score, reverse=True)
    return a, b, c


def decide_outcome(
    *, has_steps: bool, level: str, bucket_a: list[Candidate], bucket_b: list[Candidate]
) -> Outcome:
    if has_steps and level in {"high", "medium"}:
        return Outcome.RESOLUTION_FOUND
    if bucket_a:
        return Outcome.INSUFFICIENT_EVIDENCE
    if bucket_b:
        return Outcome.SIMILAR_NO_RESOLUTION_RECORDED
    return Outcome.NO_HISTORY


def is_major_incident(
    bucket_c: list[Candidate], settings: Settings, *, now: datetime | None = None
) -> bool:
    """Counts the current ticket plus open matches opened inside the window."""

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.major_incident_window_hours)
    recent = sum(
        1
        for c in bucket_c
        if c.opened_at and c.opened_at >= cutoff and c.relation == "duplicate"
    )
    return (recent + 1) >= settings.major_incident_min_tickets


def similar_without_resolution(bucket_b: list[Candidate], limit: int = 5) -> list[SimilarNoResolution]:
    return [
        SimilarNoResolution(
            number=c.number,
            table=c.table,
            sys_id=c.sys_id,
            resolved_by=c.resolved_by,
            assignment_group=c.assignment_group,
            resolved_at=c.resolved_at,
            resolution_level=c.resolution_level.value,
        )
        for c in bucket_b[:limit]
    ]


def active_related(
    bucket_c: list[Candidate], drafts: dict[str, tuple[str, str]], limit: int = 5
) -> list[ActiveRelated]:
    """`drafts` maps ticket number -> (relation, tried_so_far) from the LLM. The
    facts around it come from the record, not from the model."""

    out: list[ActiveRelated] = []
    for candidate in bucket_c[:limit]:
        relation, tried = drafts.get(candidate.number, (candidate.relation, ""))
        out.append(
            ActiveRelated(
                number=candidate.number,
                table=candidate.table,
                sys_id=candidate.sys_id,
                state_label=candidate.state_label,
                assigned_to=candidate.assigned_to,
                assignment_group=candidate.assignment_group,
                opened_at=candidate.opened_at,
                relation=relation if relation in {"duplicate", "possibly_related"} else candidate.relation,
                tried_so_far=tried,
            )
        )
    return out
