"""Core domain models: tickets as they come out of ServiceNow, and the indexed
chunks that citations point at."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

SN_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_sn_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), SN_DATETIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ResolutionLevel(str, Enum):
    """How much usable resolution information a closed ticket actually carries."""

    RICH = "rich"  # describes what was done or what caused it
    WEAK = "weak"  # "fixed", "resolved", "working now"
    NONE = "none"  # nothing recorded at all


class JournalEntry(BaseModel):
    element: str = "work_notes"
    value: str = ""
    created_on: datetime | None = None
    created_by: str = ""


class Ticket(BaseModel):
    sys_id: str
    number: str
    table: str = "incident"
    short_description: str = ""
    description: str = ""
    state: str = ""
    state_label: str = ""
    active: bool = True
    priority: str = ""
    category: str = ""
    subcategory: str = ""
    cmdb_ci: str = ""
    cmdb_ci_id: str = ""
    business_service: str = ""
    assignment_group: str = ""
    assigned_to: str = ""
    resolved_by: str = ""
    opened_at: datetime | None = None
    resolved_at: datetime | None = None
    updated_on: datetime | None = None
    close_code: str = ""
    close_notes: str = ""
    # problem-only fields: cause_notes, fix_notes, workaround
    extra_resolution: dict[str, str] = Field(default_factory=dict)
    reopen_count: int = 0
    problem_id: str = ""
    change_ids: list[str] = Field(default_factory=list)
    journal: list[JournalEntry] = Field(default_factory=list)

    @property
    def priority_rank(self) -> int:
        """1 is most urgent. `priority` holds the display value ("1 - Critical")."""

        match = re.match(r"\s*(\d)", self.priority or "")
        return int(match.group(1)) if match else 5

    @property
    def work_notes(self) -> list[JournalEntry]:
        return [j for j in self.journal if j.element == "work_notes"]

    @property
    def resolution_text(self) -> str:
        parts = [self.close_notes, *self.extra_resolution.values()]
        return "\n".join(p.strip() for p in parts if p and p.strip())

    def matching_text(self) -> str:
        return f"{self.short_description}\n{self.description}".strip()


class Chunk(BaseModel):
    """One citable piece of text. `evidence_id` is what the LLM cites."""

    evidence_id: str
    sys_id: str
    number: str
    table: str
    field: str
    text: str
    created_on: datetime | None = None
    category: str = ""
    cmdb_ci: str = ""
    business_service: str = ""
    assignment_group: str = ""
    resolved_by: str = ""
    resolved_at: datetime | None = None
    resolution_level: ResolutionLevel = ResolutionLevel.NONE
    reopen_count: int = 0
    problem_id: str = ""
    change_ids: list[str] = Field(default_factory=list)
    active: bool = False
    state_label: str = ""
    assigned_to: str = ""
    opened_at: datetime | None = None


class ScoredChunk(BaseModel):
    chunk: Chunk
    score: float = 0.0


class Bucket(str, Enum):
    """Which role a candidate ticket may play in the answer."""

    A_RESOLVED_WITH_INFO = "A"  # may support resolution steps
    B_RESOLVED_NO_INFO = "B"  # same issue before, but no fix written down
    C_ACTIVE = "C"  # open right now: duplicate / ongoing


class Candidate(BaseModel):
    """A ticket found during retrieval, with every chunk we hold for it."""

    number: str
    sys_id: str
    table: str
    chunks: list[Chunk]
    retrieval_score: float = 0.0
    is_open: bool = False
    resolution_level: ResolutionLevel = ResolutionLevel.NONE
    category: str = ""
    cmdb_ci: str = ""
    business_service: str = ""
    assignment_group: str = ""
    assigned_to: str = ""
    resolved_by: str = ""
    resolved_at: datetime | None = None
    opened_at: datetime | None = None
    state_label: str = ""
    reopen_count: int = 0
    # filled in by grading
    relevance: str = ""
    grade_score: float = 0.0
    relation: str = "unrelated"
    bucket: Bucket | None = None

    @property
    def evidence_ids(self) -> list[str]:
        return [c.evidence_id for c in self.chunks]

    @classmethod
    def from_chunks(cls, chunks: list[Chunk], retrieval_score: float = 0.0) -> "Candidate":
        head = chunks[0]
        return cls(
            number=head.number,
            sys_id=head.sys_id,
            table=head.table,
            chunks=chunks,
            retrieval_score=retrieval_score,
            is_open=head.active,
            resolution_level=head.resolution_level,
            category=head.category,
            cmdb_ci=head.cmdb_ci,
            business_service=head.business_service,
            assignment_group=head.assignment_group,
            assigned_to=head.assigned_to,
            resolved_by=head.resolved_by,
            resolved_at=head.resolved_at,
            opened_at=head.opened_at,
            state_label=head.state_label,
            reopen_count=head.reopen_count,
        )


class SearchScope(BaseModel):
    """How wide to cast the net. The loop widens this when a pass finds nothing
    worth citing: scoped to the component, then the category, then unscoped."""

    level: int = 0
    recency_days: int = 1095
    as_of: datetime | None = None  # evaluation only: hide anything newer
