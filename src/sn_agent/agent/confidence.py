"""Confidence scoring.

The score is a weighted mix of signals that can be measured, not the model's
opinion of itself — the model's self-rating is one small input among six. Caps
sit on top so an answer can never be called reliable while its claims are poorly
supported.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..config import Settings
from ..models import Candidate, ResolutionLevel
from .schemas import ConfidenceReport, VerificationReport, VerifiedClaim

CORROBORATION_TARGET = 3
NO_STEPS_CAP = 40.0


def compute_confidence(
    *,
    settings: Settings,
    bucket_a: list[Candidate],
    steps: list[VerifiedClaim],
    root_cause: VerifiedClaim | None,
    verification: VerificationReport,
    self_confidence: float,
    agreement: float | None = None,
    now: datetime | None = None,
) -> ConfidenceReport:
    now = now or datetime.now(timezone.utc)
    cited_numbers = _cited_numbers(steps, root_cause)
    cited = [c for c in bucket_a if c.number in cited_numbers] or bucket_a

    signals: dict[str, float] = {
        "retrieval_strength": _retrieval_strength(bucket_a),
        "corroboration": min(len(cited_numbers) / CORROBORATION_TARGET, 1.0),
        "citation_support": max(0.0, min(1.0, verification.support_rate)),
        "source_quality": _source_quality(cited, now),
        "self_rating": max(0.0, min(1.0, self_confidence)),
    }
    if agreement is not None:
        signals["agreement"] = max(0.0, min(1.0, agreement))

    weights = {k: v for k, v in settings.confidence_weights.items() if k in signals}
    total_weight = sum(weights.values()) or 1.0
    score = 100.0 * sum(signals[k] * w for k, w in weights.items()) / total_weight

    caps: list[str] = []
    if not steps:
        score = min(score, NO_STEPS_CAP)
        caps.append("no_resolution_steps")
    if verification.support_rate < settings.min_support_rate_for_high:
        score = min(score, settings.confidence_high - 1)
        caps.append("low_citation_support")

    return ConfidenceReport(
        score=round(score, 1),
        level=level_for(score, settings),
        signals={k: round(v, 3) for k, v in signals.items()},
        weights={k: round(w / total_weight, 3) for k, w in weights.items()},
        caps_applied=caps,
    )


def level_for(score: float, settings: Settings) -> str:
    if score >= settings.confidence_high:
        return "high"
    if score >= settings.confidence_medium:
        return "medium"
    return "low"


def _cited_numbers(steps: list[VerifiedClaim], root_cause: VerifiedClaim | None) -> set[str]:
    claims = [*steps, *( [root_cause] if root_cause else [] )]
    return {eid.split(":", 1)[0] for claim in claims for eid in claim.evidence_ids}


def _retrieval_strength(bucket_a: list[Candidate]) -> float:
    if not bucket_a:
        return 0.0
    scores = sorted((c.grade_score for c in bucket_a), reverse=True)
    top = scores[0]
    top3 = sum(scores[:3]) / len(scores[:3])
    return 0.6 * top + 0.4 * top3


def _source_quality(candidates: list[Candidate], now: datetime) -> float:
    if not candidates:
        return 0.0
    total = 0.0
    for candidate in candidates:
        level = 1.0 if candidate.resolution_level == ResolutionLevel.RICH else 0.4
        reopened = 1.0 if candidate.reopen_count == 0 else 0.6
        total += level * reopened * _recency(candidate.resolved_at or candidate.opened_at, now)
    return total / len(candidates)


def _recency(when: datetime | None, now: datetime) -> float:
    if when is None:
        return 0.7
    age_days = (now - when).days
    if age_days <= 365:
        return 1.0
    if age_days <= 1095:
        return 0.8
    return 0.6


def agreement_score(sample_numbers: list[set[str]]) -> float:
    """Jaccard overlap of the ticket numbers cited across repeated generations.
    Answers that keep landing on the same past tickets are more trustworthy than
    ones that wander."""

    if len(sample_numbers) < 2:
        return 0.0
    pairs = [
        (a, b) for i, a in enumerate(sample_numbers) for b in sample_numbers[i + 1 :]
    ]
    scores = []
    for a, b in pairs:
        union = a | b
        scores.append(len(a & b) / len(union) if union else 0.0)
    return sum(scores) / len(scores) if scores else 0.0
