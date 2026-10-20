"""The rules that stop a plausible-sounding but unsupported answer going out."""

from __future__ import annotations

from sn_agent.agent.schemas import ActiveRelatedDraft, CitedClaim, ResolutionDraft, SupportBatch
from sn_agent.agent.verify import verify_draft
from sn_agent.config import Settings
from sn_agent.ingest.chunking import chunk_ticket
from sn_agent.llm import Meter
from sn_agent.models import ResolutionLevel

from .factories import make_ticket
from .fakes import FakeLLM, support_handler


def _evidence():
    resolved = make_ticket("INC0010001", close_notes="Re-issued the VPN certificate.")
    weak = make_ticket("INC0010002", close_notes="Fixed")
    open_ticket = make_ticket("INC0010003", active=True, work_notes=["Restarted the gateway, no change."])
    chunks = {
        c.evidence_id: c
        for ticket, level in ((resolved, ResolutionLevel.RICH), (weak, ResolutionLevel.WEAK), (open_ticket, ResolutionLevel.NONE))
        for c in chunk_ticket(ticket, level)
    }
    fix_ids = {eid for eid, c in chunks.items() if c.number == "INC0010001"}
    return chunks, fix_ids


def _meter():
    return Meter(Settings().prices, 10.0, 10**6)


def test_fabricated_citation_is_removed():
    evidence, fix_ids = _evidence()
    draft = ResolutionDraft(
        resolution_steps=[
            CitedClaim(text="Re-issue the certificate", evidence_ids=["INC0010001:close_notes:0"]),
            CitedClaim(text="Reboot the datacentre", evidence_ids=["INC9999999:close_notes:0"]),
        ],
        self_confidence=0.9,
    )
    llm = FakeLLM({"verify_support": support_handler("supported")})

    verified, report = verify_draft(
        draft, evidence=evidence, fix_evidence_ids=fix_ids, open_numbers=set(), llm=llm, meter=_meter()
    )

    assert [s.text for s in verified.resolution_steps] == ["Re-issue the certificate"]
    assert "step_2" in report.removed_invalid_citation


def test_ticket_without_recorded_resolution_cannot_support_a_fix_step():
    evidence, fix_ids = _evidence()
    draft = ResolutionDraft(
        resolution_steps=[CitedClaim(text="Do what INC0010002 did", evidence_ids=["INC0010002:close_notes:0"])],
        other_claims=[CitedClaim(text="Seen before in INC0010002", evidence_ids=["INC0010002:description:0"])],
    )
    llm = FakeLLM({"verify_support": support_handler("supported")})

    verified, report = verify_draft(
        draft, evidence=evidence, fix_evidence_ids=fix_ids, open_numbers=set(), llm=llm, meter=_meter()
    )

    assert verified.resolution_steps == []
    assert "step_1" in report.removed_wrong_bucket
    # Context claims may still cite it.
    assert len(verified.other_claims) == 1


def test_unsupported_claim_is_dropped_and_lowers_the_support_rate():
    evidence, fix_ids = _evidence()
    draft = ResolutionDraft(
        resolution_steps=[
            CitedClaim(text="Re-issue the certificate", evidence_ids=["INC0010001:close_notes:0"]),
            CitedClaim(text="Also upgrade the firmware", evidence_ids=["INC0010001:close_notes:0"]),
        ]
    )
    llm = FakeLLM({"verify_support": support_handler("supported", {"step_2": "unsupported"})})

    verified, report = verify_draft(
        draft, evidence=evidence, fix_evidence_ids=fix_ids, open_numbers=set(), llm=llm, meter=_meter()
    )

    assert len(verified.resolution_steps) == 1
    assert report.removed_unsupported == ["step_2"]
    assert report.support_rate == 0.5


def test_active_related_must_have_been_retrieved():
    evidence, fix_ids = _evidence()
    draft = ResolutionDraft(
        active_related=[
            ActiveRelatedDraft(number="INC0010003", relation="duplicate", tried_so_far="restarted gateway"),
            ActiveRelatedDraft(number="INC0004444", relation="duplicate", tried_so_far="invented"),
        ]
    )
    llm = FakeLLM({"verify_support": support_handler("supported")})

    verified, report = verify_draft(
        draft, evidence=evidence, fix_evidence_ids=fix_ids, open_numbers={"INC0010003"}, llm=llm, meter=_meter()
    )

    assert set(verified.active_related) == {"INC0010003"}
    assert report.dropped_active_related == ["INC0004444"]


def test_claim_the_verifier_ignored_counts_as_unsupported():
    evidence, fix_ids = _evidence()
    draft = ResolutionDraft(
        resolution_steps=[CitedClaim(text="Re-issue the certificate", evidence_ids=["INC0010001:close_notes:0"])]
    )
    llm = FakeLLM({"verify_support": lambda prompt: SupportBatch()})

    verified, report = verify_draft(
        draft, evidence=evidence, fix_evidence_ids=fix_ids, open_numbers=set(), llm=llm, meter=_meter()
    )

    assert verified.resolution_steps == []
    assert report.support_rate == 0.0
