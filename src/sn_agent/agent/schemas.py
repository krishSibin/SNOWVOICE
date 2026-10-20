"""Schemas for what the LLM returns, what verification produces, and the final
run result."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# Fixed vocabularies. As Literal types they become `enum` in the JSON schema,
# so the model can only answer with one of these, and anything else fails validation.
Relevance = Literal["relevant", "partial", "irrelevant"]
Relation = Literal["duplicate", "possibly_related", "unrelated"]
SupportLabel = Literal["supported", "partial", "unsupported"]
MatchLabel = Literal["full", "partial", "none"]

# --- LLM step outputs -------------------------------------------------------


class TicketFacts(BaseModel):
    summary: str = ""
    symptoms: list[str] = Field(default_factory=list)
    error_codes: list[str] = Field(default_factory=list)
    affected_component: str = ""
    environment: str = ""
    scope: str = ""
    keywords: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    queries: list[str] = Field(default_factory=list)


class TicketGrade(BaseModel):
    number: str
    relevance: Relevance = "irrelevant"
    score: float = 0.0
    relation: Relation = "unrelated"
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _untangle_fields(cls, data):
        """Smaller models sometimes answer `relevance: "duplicate"` for an open ticket.
        The meaning is clear, so move it to `relation` rather than fail the whole batch."""

        if not isinstance(data, dict):
            return data
        data = dict(data)
        relevance = str(data.get("relevance", "")).strip().lower().replace(" ", "_")
        if relevance in {"duplicate", "possibly_related", "related"}:
            data["relevance"] = "relevant"
            if data.get("relation") in (None, "", "unrelated"):
                data["relation"] = "possibly_related" if relevance == "related" else relevance
        elif relevance == "unrelated":
            data["relevance"] = "irrelevant"
        return data


class GradeBatch(BaseModel):
    grades: list[TicketGrade] = Field(default_factory=list)


class CitedClaim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class ActiveRelatedDraft(BaseModel):
    number: str
    relation: Literal["duplicate", "possibly_related"] = "possibly_related"
    tried_so_far: str = ""


class ResolutionDraft(BaseModel):
    root_cause: CitedClaim = Field(default_factory=lambda: CitedClaim(text=""))
    resolution_steps: list[CitedClaim] = Field(default_factory=list)
    other_claims: list[CitedClaim] = Field(default_factory=list)
    active_related: list[ActiveRelatedDraft] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    diagnostic_questions: list[str] = Field(default_factory=list)
    self_confidence: float = 0.0


class ClaimSupport(BaseModel):
    claim_id: str
    label: SupportLabel = "unsupported"
    reason: str = ""


class SupportBatch(BaseModel):
    results: list[ClaimSupport] = Field(default_factory=list)


class Diagnostics(BaseModel):
    """Produced when no documented fix exists, so the note still helps."""

    gaps: list[str] = Field(default_factory=list)
    diagnostic_questions: list[str] = Field(default_factory=list)


class ResolutionMatch(BaseModel):
    """Used by the offline evaluation only."""

    match: MatchLabel = "none"
    reason: str = ""


# --- Verified / final output ------------------------------------------------


class VerifiedClaim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    # Why a human should check this step before following it (see agent/safety.py).
    flags: list[str] = Field(default_factory=list)
    support: str = "supported"  # supported | partial


class VerificationReport(BaseModel):
    claims_generated: int = 0
    claims_kept: int = 0
    removed_invalid_citation: list[str] = Field(default_factory=list)
    removed_wrong_bucket: list[str] = Field(default_factory=list)
    removed_unsupported: list[str] = Field(default_factory=list)
    partial_claims: list[str] = Field(default_factory=list)
    dropped_active_related: list[str] = Field(default_factory=list)
    support_rate: float = 0.0


class ConfidenceReport(BaseModel):
    score: float = 0.0
    level: str = "low"  # high | medium | low
    signals: dict[str, float] = Field(default_factory=dict)
    weights: dict[str, float] = Field(default_factory=dict)
    caps_applied: list[str] = Field(default_factory=list)


class SimilarNoResolution(BaseModel):
    number: str
    table: str = "incident"
    sys_id: str = ""
    resolved_by: str = ""
    assignment_group: str = ""
    resolved_at: datetime | None = None
    resolution_level: str = "none"


class ActiveRelated(BaseModel):
    number: str
    table: str = "incident"
    sys_id: str = ""
    state_label: str = ""
    assigned_to: str = ""
    assignment_group: str = ""
    opened_at: datetime | None = None
    relation: str = "possibly_related"
    tried_so_far: str = ""


class EvidenceRef(BaseModel):
    evidence_id: str
    number: str
    table: str
    sys_id: str
    field: str
    snippet: str = ""


class Outcome(str, Enum):
    RESOLUTION_FOUND = "RESOLUTION_FOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    SIMILAR_NO_RESOLUTION_RECORDED = "SIMILAR_NO_RESOLUTION_RECORDED"
    NO_HISTORY = "NO_HISTORY"


class RunResult(BaseModel):
    run_id: str
    ticket_number: str
    ticket_sys_id: str
    table: str = "incident"
    outcome: Outcome = Outcome.NO_HISTORY
    active_issue_in_progress: bool = False
    major_incident_warning: bool = False
    confidence: ConfidenceReport = Field(default_factory=ConfidenceReport)
    verification: VerificationReport = Field(default_factory=VerificationReport)
    root_cause: VerifiedClaim | None = None
    resolution_steps: list[VerifiedClaim] = Field(default_factory=list)
    other_claims: list[VerifiedClaim] = Field(default_factory=list)
    similar_without_resolution: list[SimilarNoResolution] = Field(default_factory=list)
    active_related: list[ActiveRelated] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    diagnostic_questions: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    iterations: int = 0
    budget_exhausted: bool = False
    stopped_early: str = ""  # why research ended before it was finished
    cost_usd: float = 0.0
    tokens: int = 0
    latency_s: float = 0.0
    models_used: list[str] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
