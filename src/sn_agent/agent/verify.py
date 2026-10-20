"""Verification: the part that keeps the agent honest.

Two passes. First code checks every citation exists and comes from evidence
allowed to support that kind of claim. Then a separate LLM call checks whether
the cited text actually says what the claim says. The generator never grades its
own work.
"""

from __future__ import annotations

from ..llm import LLMClient, Meter, Tier
from ..models import Chunk
from .prompts.verify import CLAIM_BLOCK, SUPPORT_PROMPT, SUPPORT_SYSTEM
from .schemas import (
    CitedClaim,
    ResolutionDraft,
    SupportBatch,
    VerificationReport,
    VerifiedClaim,
)

MAX_EVIDENCE_CHARS = 1200


class VerifiedDraft:
    def __init__(self) -> None:
        self.root_cause: VerifiedClaim | None = None
        self.resolution_steps: list[VerifiedClaim] = []
        self.other_claims: list[VerifiedClaim] = []
        self.active_related: dict[str, tuple[str, str]] = {}


def verify_draft(
    draft: ResolutionDraft,
    *,
    evidence: dict[str, Chunk],
    fix_evidence_ids: set[str],
    open_numbers: set[str],
    llm: LLMClient,
    meter: Meter,
) -> tuple[VerifiedDraft, VerificationReport]:
    report = VerificationReport()
    pending: list[tuple[str, CitedClaim, str]] = []  # (claim_id, claim, kind)

    def stage(claim: CitedClaim, claim_id: str, kind: str) -> None:
        report.claims_generated += 1
        text = claim.text.strip()
        if not text:
            return
        valid = [eid for eid in claim.evidence_ids if eid in evidence]
        if len(valid) < len(claim.evidence_ids):
            report.removed_invalid_citation.append(claim_id)
        if kind in {"root_cause", "step"}:
            allowed = [eid for eid in valid if eid in fix_evidence_ids]
            if len(allowed) < len(valid):
                report.removed_wrong_bucket.append(claim_id)
            valid = allowed
        if not valid:
            if claim_id not in report.removed_invalid_citation and claim_id not in report.removed_wrong_bucket:
                report.removed_invalid_citation.append(claim_id)
            return
        pending.append((claim_id, CitedClaim(text=text, evidence_ids=valid), kind))

    if draft.root_cause and draft.root_cause.text.strip():
        stage(draft.root_cause, "root_cause", "root_cause")
    for index, step in enumerate(draft.resolution_steps, start=1):
        stage(step, f"step_{index}", "step")
    for index, claim in enumerate(draft.other_claims, start=1):
        stage(claim, f"other_{index}", "other")

    labels = _support_labels(pending, evidence, llm=llm, meter=meter) if pending else {}

    verified = VerifiedDraft()
    supported_weight = 0.0
    for claim_id, claim, kind in pending:
        label = labels.get(claim_id, "unsupported")
        if label == "unsupported":
            report.removed_unsupported.append(claim_id)
            continue
        if label == "partial":
            report.partial_claims.append(claim_id)
            supported_weight += 0.5
        else:
            supported_weight += 1.0
        verified_claim = VerifiedClaim(text=claim.text, evidence_ids=claim.evidence_ids, support=label)
        if kind == "root_cause":
            verified.root_cause = verified_claim
        elif kind == "step":
            verified.resolution_steps.append(verified_claim)
        else:
            verified.other_claims.append(verified_claim)

    for entry in draft.active_related:
        if entry.number in open_numbers:
            verified.active_related[entry.number] = (entry.relation, entry.tried_so_far.strip())
        else:
            report.dropped_active_related.append(entry.number)

    report.claims_kept = (
        len(verified.resolution_steps) + len(verified.other_claims) + (1 if verified.root_cause else 0)
    )
    report.support_rate = supported_weight / report.claims_generated if report.claims_generated else 0.0
    return verified, report


def _support_labels(
    pending: list[tuple[str, CitedClaim, str]],
    evidence: dict[str, Chunk],
    *,
    llm: LLMClient,
    meter: Meter,
) -> dict[str, str]:
    blocks = []
    for claim_id, claim, _kind in pending:
        cited = "\n".join(
            f"[{eid}] {evidence[eid].text[:MAX_EVIDENCE_CHARS]}" for eid in claim.evidence_ids
        )
        blocks.append(CLAIM_BLOCK.format(claim_id=claim_id, text=claim.text, evidence=cited))
    batch = llm.generate_json(
        tier=Tier.FLASH,
        system=SUPPORT_SYSTEM,
        prompt=SUPPORT_PROMPT.format(claims="\n\n---\n\n".join(blocks)),
        schema=SupportBatch,
        meter=meter,
        step="verify_support",
    )
    labels = {r.claim_id: r.label for r in batch.results if r.label in {"supported", "partial", "unsupported"}}
    # A claim the verifier ignored is treated as unsupported, never as fine.
    return {claim_id: labels.get(claim_id, "unsupported") for claim_id, _c, _k in pending}
