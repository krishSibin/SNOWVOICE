"""Decide how much usable resolution information a closed ticket carries.

Rules first, because they are free and predictable. The LLM is asked only about
the genuinely unclear middle, and only when one is supplied.
"""

from __future__ import annotations

import re

from ..agent.prompts.classify import RESOLUTION_LEVEL_PROMPT, RESOLUTION_LEVEL_SYSTEM
from typing import Literal

from pydantic import BaseModel

from ..llm import LLMClient, Meter, Tier
from ..models import ResolutionLevel, Ticket
from .redact import redact

WEAK_PATTERNS = re.compile(
    r"(?i)^\W*("
    r"fixed|resolved|done|closed|completed|complete|ok|okay|na|n/?a|none|nil|test|"
    r"issue resolved|resolved now|working( now| fine)?|works now|no issue(s)? found|"
    r"user confirmed|confirmed by user|as per user|as discussed|per user request|"
    r"auto[- ]?closed|closed by system|duplicate|no response from user|solved"
    r")\W*$"
)
MIN_RICH_CHARS = 60
MIN_UNCLEAR_CHARS = 25
MIN_SUBSTANTIVE_NOTE_CHARS = 60


class _LevelVerdict(BaseModel):
    level: Literal["rich", "weak", "none"] = "weak"
    reason: str = ""


def substantive_work_notes(ticket: Ticket) -> list[str]:
    return [
        n.value.strip()
        for n in ticket.work_notes
        if len(n.value.strip()) >= MIN_SUBSTANTIVE_NOTE_CHARS and not WEAK_PATTERNS.match(n.value.strip())
    ]


def classify_resolution_level(
    ticket: Ticket, *, llm: LLMClient | None = None, meter: Meter | None = None
) -> ResolutionLevel:
    resolution = ticket.resolution_text.strip()
    notes = substantive_work_notes(ticket)

    if len(resolution) >= MIN_RICH_CHARS and not WEAK_PATTERNS.match(resolution):
        return ResolutionLevel.RICH
    if not resolution and not notes:
        return ResolutionLevel.NONE
    if not notes and (WEAK_PATTERNS.match(resolution) or len(resolution) < MIN_UNCLEAR_CHARS):
        return ResolutionLevel.WEAK if resolution else ResolutionLevel.NONE

    if llm is None or meter is None:
        # Without a classifier, assume the weaker reading: it can never be cited
        # for a fix, which is the safe failure direction.
        return ResolutionLevel.WEAK if (resolution or notes) else ResolutionLevel.NONE

    evidence = "\n\n".join(
        [
            f"CLOSE NOTES:\n{redact(resolution) or '(empty)'}",
            *[f"WORK NOTE:\n{redact(n)}" for n in notes[-3:]],
        ]
    )
    verdict = llm.generate_json(
        tier=Tier.LITE,
        system=RESOLUTION_LEVEL_SYSTEM,
        prompt=RESOLUTION_LEVEL_PROMPT.format(evidence=evidence[:4000]),
        schema=_LevelVerdict,
        meter=meter,
        step="resolution_level",
    )
    try:
        return ResolutionLevel(verdict.level.strip().lower())
    except ValueError:
        return ResolutionLevel.WEAK
