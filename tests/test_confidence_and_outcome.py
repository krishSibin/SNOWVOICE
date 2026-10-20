from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sn_agent.agent.confidence import agreement_score, compute_confidence
from sn_agent.agent.outcome import (
    apply_grades,
    decide_outcome,
    is_major_incident,
    split_buckets,
)
from sn_agent.agent.schemas import Outcome, TicketGrade, VerificationReport, VerifiedClaim
from sn_agent.config import Settings
from sn_agent.ingest.chunking import chunk_ticket
from sn_agent.models import Bucket, Candidate, ResolutionLevel

from .factories import NOW, make_ticket

SETTINGS = Settings()


def candidate(number: str, *, level=ResolutionLevel.RICH, open_ticket=False, days_ago=30, reopen=0) -> Candidate:
    ticket = make_ticket(number, active=open_ticket, days_ago=days_ago, reopen_count=reopen, close_notes="Replaced the certificate on the gateway.")
    return Candidate.from_chunks(chunk_ticket(ticket, level), 1.0)


def report(support_rate: float = 1.0) -> VerificationReport:
    return VerificationReport(claims_generated=2, claims_kept=2, support_rate=support_rate)


def steps(*evidence_ids: str) -> list[VerifiedClaim]:
    return [VerifiedClaim(text=f"step for {eid}", evidence_ids=[eid]) for eid in evidence_ids]


def test_more_corroborating_tickets_raises_the_score():
    bucket = [candidate("INC1"), candidate("INC2"), candidate("INC3")]
    for c in bucket:
        c.grade_score = 0.9

    one = compute_confidence(
        settings=SETTINGS, bucket_a=bucket, steps=steps("INC1:close_notes:0"), root_cause=None,
        verification=report(), self_confidence=0.8, now=NOW,
    )
    three = compute_confidence(
        settings=SETTINGS, bucket_a=bucket,
        steps=steps("INC1:close_notes:0", "INC2:close_notes:0", "INC3:close_notes:0"),
        root_cause=None, verification=report(), self_confidence=0.8, now=NOW,
    )
    assert three.score > one.score


def test_weak_citation_support_caps_the_score_below_high():
    bucket = [candidate("INC1")]
    bucket[0].grade_score = 1.0
    result = compute_confidence(
        settings=SETTINGS, bucket_a=bucket, steps=steps("INC1:close_notes:0"), root_cause=None,
        verification=report(support_rate=0.2), self_confidence=1.0, now=NOW,
    )
    assert result.score < SETTINGS.confidence_high
    assert "low_citation_support" in result.caps_applied


def test_no_steps_means_low_confidence():
    bucket = [candidate("INC1")]
    bucket[0].grade_score = 1.0
    result = compute_confidence(
        settings=SETTINGS, bucket_a=bucket, steps=[], root_cause=None,
        verification=report(support_rate=1.0), self_confidence=1.0, now=NOW,
    )
    assert result.level == "low"
    assert "no_resolution_steps" in result.caps_applied


def test_old_and_reopened_sources_score_lower_than_recent_clean_ones():
    fresh = candidate("INC1", days_ago=10)
    stale = candidate("INC2", days_ago=1500, reopen=2)
    fresh.grade_score = stale.grade_score = 0.9

    good = compute_confidence(
        settings=SETTINGS, bucket_a=[fresh], steps=steps("INC1:close_notes:0"), root_cause=None,
        verification=report(), self_confidence=0.7, now=NOW,
    )
    bad = compute_confidence(
        settings=SETTINGS, bucket_a=[stale], steps=steps("INC2:close_notes:0"), root_cause=None,
        verification=report(), self_confidence=0.7, now=NOW,
    )
    assert good.score > bad.score


def test_agreement_score_rewards_repeated_answers():
    assert agreement_score([{"INC1"}, {"INC1"}]) == 1.0
    assert agreement_score([{"INC1"}, {"INC2"}]) == 0.0


def test_bucketing_splits_documented_undocumented_and_open():
    documented = candidate("INC1")
    undocumented = candidate("INC2", level=ResolutionLevel.WEAK)
    open_ticket = candidate("INC3", open_ticket=True, level=ResolutionLevel.NONE)
    noise = candidate("INC4")

    kept = apply_grades(
        [documented, undocumented, open_ticket, noise],
        [
            TicketGrade(number="INC1", relevance="relevant", score=0.9),
            TicketGrade(number="INC2", relevance="relevant", score=0.8),
            TicketGrade(number="INC3", relevance="relevant", score=0.85, relation="duplicate"),
            TicketGrade(number="INC4", relevance="irrelevant", score=0.1),
        ],
    )
    a, b, c = split_buckets(kept)

    assert [x.number for x in a] == ["INC1"]
    assert [x.number for x in b] == ["INC2"]
    assert [x.number for x in c] == ["INC3"]
    assert all(x.bucket is not None for x in kept)
    assert "INC4" not in {x.number for x in kept}


def test_open_ticket_graded_unrelated_is_dropped():
    open_ticket = candidate("INC3", open_ticket=True, level=ResolutionLevel.NONE)
    kept = apply_grades([open_ticket], [TicketGrade(number="INC3", relevance="relevant", score=0.9, relation="unrelated")])
    assert kept == []


def test_major_incident_warning_needs_enough_recent_duplicates():
    recent = []
    for index in range(2):
        c = candidate(f"INC{index}", open_ticket=True, level=ResolutionLevel.NONE, days_ago=0)
        c.relation = "duplicate"
        c.bucket = Bucket.C_ACTIVE
        c.opened_at = datetime.now(timezone.utc) - timedelta(hours=2)
        recent.append(c)

    assert is_major_incident(recent, SETTINGS) is True  # two others plus this ticket
    assert is_major_incident(recent[:1], SETTINGS) is False


def test_outcome_rules():
    documented = candidate("INC1")
    undocumented = candidate("INC2", level=ResolutionLevel.WEAK)

    assert decide_outcome(has_steps=True, level="high", bucket_a=[documented], bucket_b=[]) == Outcome.RESOLUTION_FOUND
    assert decide_outcome(has_steps=False, level="low", bucket_a=[documented], bucket_b=[]) == Outcome.INSUFFICIENT_EVIDENCE
    assert decide_outcome(has_steps=False, level="low", bucket_a=[], bucket_b=[undocumented]) == Outcome.SIMILAR_NO_RESOLUTION_RECORDED
    assert decide_outcome(has_steps=False, level="low", bucket_a=[], bucket_b=[]) == Outcome.NO_HISTORY


def test_grade_with_relation_in_the_relevance_field_is_untangled():
    """gpt-4o-mini sometimes answers relevance="duplicate" for an open ticket;
    that must not throw away the whole grading batch."""

    from sn_agent.agent.schemas import GradeBatch

    batch = GradeBatch.model_validate_json(
        '{"grades":['
        '{"number":"INC1","relevance":"relevant","score":1.0,"relation":"unrelated"},'
        '{"number":"INC2","relevance":"duplicate","score":0.9,"relation":"unrelated"},'
        '{"number":"INC3","relevance":"possibly_related","score":0.6}]}'
    )

    assert [(g.relevance, g.relation) for g in batch.grades] == [
        ("relevant", "unrelated"),
        ("relevant", "duplicate"),
        ("relevant", "possibly_related"),
    ]
