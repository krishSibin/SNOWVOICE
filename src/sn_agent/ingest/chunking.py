"""Split a ticket into citable chunks.

`evidence_id` is `<number>:<field>:<index>` — stable across syncs, readable in a
work note, and enough on its own to check a citation.
"""

from __future__ import annotations

from ..models import Chunk, ResolutionLevel, Ticket
from .redact import redact

CITABLE_FIELDS = ("description", "close_notes", "work_notes", "comments")


def split_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= max_chars:
            current = f"{current}\n\n{paragraph}" if current else paragraph
            continue
        if current:
            parts.append(current)
        while len(paragraph) > max_chars:
            cut = paragraph.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            parts.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        current = paragraph
    if current:
        parts.append(current)
    return [p for p in parts if p]


def chunk_ticket(
    ticket: Ticket, resolution_level: ResolutionLevel, *, max_chars: int = 1500
) -> list[Chunk]:
    def make(field: str, index: int, text: str) -> Chunk:
        return Chunk(
            evidence_id=f"{ticket.number}:{field}:{index}",
            sys_id=ticket.sys_id,
            number=ticket.number,
            table=ticket.table,
            field=field,
            text=text,
            created_on=ticket.opened_at,
            category=ticket.category,
            cmdb_ci=ticket.cmdb_ci,
            business_service=ticket.business_service,
            assignment_group=ticket.assignment_group,
            resolved_by=ticket.resolved_by,
            resolved_at=ticket.resolved_at,
            resolution_level=resolution_level,
            reopen_count=ticket.reopen_count,
            problem_id=ticket.problem_id,
            change_ids=list(ticket.change_ids),
            active=ticket.active,
            state_label=ticket.state_label,
            assigned_to=ticket.assigned_to,
            opened_at=ticket.opened_at,
        )

    chunks: list[Chunk] = []

    header = redact(f"{ticket.short_description}\n\n{ticket.description}".strip())
    for i, part in enumerate(split_text(header, max_chars)):
        chunks.append(make("description", i, part))

    resolution_parts: list[tuple[str, str]] = []
    if ticket.close_notes.strip():
        resolution_parts.append(("close_notes", ticket.close_notes))
    for field, value in ticket.extra_resolution.items():
        if value.strip():
            resolution_parts.append((field, value))
    for field, value in resolution_parts:
        for i, part in enumerate(split_text(redact(value), max_chars)):
            chunk = make(field, i, part)
            chunk.created_on = ticket.resolved_at or ticket.opened_at
            chunks.append(chunk)

    for index, entry in enumerate(ticket.journal):
        text = redact(entry.value)
        if len(text) < 15:
            continue
        for i, part in enumerate(split_text(text, max_chars)):
            chunk = make(entry.element, index if i == 0 else index * 100 + i, part)
            chunk.created_on = entry.created_on or ticket.opened_at
            chunks.append(chunk)

    return chunks
