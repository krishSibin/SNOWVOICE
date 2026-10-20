"""Render a run into the work note an engineer actually reads.

Ordering is deliberate: the suggestion comes first (root cause and steps, then
side notes and next checks), then open tickets for the same issue, then evidence.
The suggestion sections carry no inline citations so they read cleanly; the
evidence section maps each point back to the tickets and fields it came from.

Layout is deliberate too. A work note is plain text in a narrow pane that does no
wrapping of its own, so anything long runs off the side or folds at random. Prose
is wrapped here at a fixed width, and evidence is grouped by ticket so each
ticket's link is printed once rather than once per quoted field.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime, timezone
from typing import Iterable, NamedTuple

from ..agent.safety import FLAG_LABELS
from ..agent.schemas import EvidenceRef, Outcome, RunResult

HEADER = "[AI Resolution Assistant]"
FINGERPRINT_LABEL = "fingerprint"

# Roughly the width of the ServiceNow activity pane before it folds a line.
WIDTH = 96

# The note is the only record we keep, so it carries what a re-run needs to know:
# which run wrote it, and a fingerprint of the ticket text it was answering.
_PREVIOUS_NOTE = re.compile(
    r"run:\s*(?P<run_id>\S+).*?" + FINGERPRINT_LABEL + r":\s*(?P<fingerprint>[0-9a-f]+)",
    re.IGNORECASE | re.DOTALL,
)


class PreviousNote(NamedTuple):
    run_id: str
    fingerprint: str


def find_previous_note(journal: Iterable) -> PreviousNote | None:
    """Reads back the agent's last note on a ticket, newest first."""

    for entry in sorted(
        [e for e in journal if e.element == "work_notes" and HEADER in (e.value or "")],
        key=lambda e: e.created_on or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    ):
        match = _PREVIOUS_NOTE.search(entry.value)
        if match:
            return PreviousNote(match.group("run_id"), match.group("fingerprint"))
    return None

OUTCOME_LINE = {
    Outcome.RESOLUTION_FOUND: "Suggested resolution found in past tickets",
    Outcome.INSUFFICIENT_EVIDENCE: "No reliable resolution found - evidence was too weak to suggest steps",
    Outcome.SIMILAR_NO_RESOLUTION_RECORDED: "Same issue seen before, but no resolution was ever recorded",
    Outcome.NO_HISTORY: "No similar past ticket found",
}


def render_note(
    result: RunResult, *, instance_url: str = "", previous_run_id: str = "", fingerprint: str = ""
) -> str:
    lines: list[str] = [f"{HEADER} {OUTCOME_LINE[result.outcome]}"]

    if result.outcome == Outcome.RESOLUTION_FOUND or result.resolution_steps:
        lines.append(f"{bold('Confidence:')} {result.confidence.score:.0f}/100 ({result.confidence.level})")
        if result.confidence.level == "medium":
            lines.append("Verify before applying: the evidence supports this only partly.")
    flagged = [c for c in [result.root_cause, *result.resolution_steps, *result.other_claims] if c and c.flags]
    if flagged:
        worst = "destructive" in {f for c in flagged for f in c.flags}
        # Left unwrapped on purpose: this is the headline warning, and folding it
        # would break the phrase a reader (and the guardrail test) scans for.
        lines.append(
            f"!! CHECK BEFORE ACTING: {len(flagged)} item(s) below contain a command or link"
            + (", and at least one could delete data or weaken security" if worst else "")
            + ". They are copied from past tickets, which may have been wrong."
        )
    if result.stopped_early:
        lines.extend(_wrap(f"Note: research stopped early ({result.stopped_early}), so this note is partial."))
    if previous_run_id:
        lines.extend(_wrap(f"This replaces an earlier note from run {previous_run_id} (ticket text changed)."))

    if result.root_cause or result.resolution_steps:
        lines.extend(_section("SUGGESTED ROOT CAUSE AND RESOLUTION"))
        if result.root_cause:
            lines.extend(_wrap(f"{bold('Likely cause:')} {result.root_cause.text}"))
            lines.extend(_flag_lines(result.root_cause.flags))
        for index, step in enumerate(result.resolution_steps, start=1):
            partial = " (partly supported)" if step.support == "partial" else ""
            lines.append("")
            lines.extend(_wrap(f"{index}. {step.text}{partial}"))
            lines.extend(_flag_lines(step.flags))

    if result.other_claims:
        lines.extend(_section("ALSO WORTH KNOWING"))
        for claim in result.other_claims:
            lines.extend(_wrap(f"- {claim.text}"))
            lines.extend(_flag_lines(claim.flags))

    if result.similar_without_resolution:
        lines.extend(_section("SAME ISSUE BEFORE, NO RESOLUTION RECORDED"))
        lines.extend(_wrap("These look like the same issue but no fix was documented. Ask whoever closed them:"))
        for entry in result.similar_without_resolution:
            lines.append("")
            lines.append(entry.number)
            lines.append(
                f"resolved {_date(entry.resolved_at)} by {entry.resolved_by or 'unknown'}"
                f" | group: {entry.assignment_group or 'unknown'}"
            )
            url = _url(instance_url, entry.table, entry.sys_id)
            if url:
                lines.append(url)

    if result.gaps:
        lines.extend(_section("GAPS"))
        for gap in result.gaps:
            lines.extend(_wrap(f"- {gap}"))

    if result.diagnostic_questions:
        lines.extend(_section("SUGGESTED NEXT CHECKS"))
        for question in result.diagnostic_questions:
            lines.extend(_wrap(f"- {question}"))

    if result.active_related:
        lines.extend(_section("ACTIVE RELATED TICKETS"))
        if result.major_incident_warning:
            lines.extend(_wrap(
                "! Several open tickets describe this same issue. Consider raising a major incident "
                "or a problem record, and coordinate before working them separately."
            ))
        for entry in result.active_related:
            label = "likely duplicate" if entry.relation == "duplicate" else "possibly related"
            owner = entry.assigned_to or entry.assignment_group or "unassigned"
            if lines[-1]:
                lines.append("")
            lines.append(f"{entry.number} ({label})")
            lines.append(
                f"state: {entry.state_label or 'open'} | with: {owner}"
                f" | opened: {_date(entry.opened_at)}"
            )
            url = _url(instance_url, entry.table, entry.sys_id)
            if url:
                lines.append(url)
            # Our own earlier note on that ticket is not something an engineer tried.
            if entry.tried_so_far and HEADER not in entry.tried_so_far:
                lines.extend(_wrap(f"already tried there: {entry.tried_so_far}"))
        lines.append("")
        lines.append("Suggested: link these tickets together and agree who drives the fix.")

    if result.evidence:
        lines.extend(_section("EVIDENCE USED"))
        lines.append("Quoted from the source tickets. Each point above is backed by:")
        for label, evidence_ids in _claim_sources(result):
            lines.append("")
            lines.extend(_wrap(f"{bold(label)} - {_citations(evidence_ids)}"))
        for number, refs in _by_ticket(result.evidence).items():
            lines.append("")
            lines.append("")
            lines.append(number)
            url = _url(instance_url, refs[0].table, refs[0].sys_id)
            if url:
                lines.append(url)
            for ref in refs:
                lines.append(_field_label(ref))
                lines.extend(_wrap(_one_line(ref.snippet)))

    lines.append("")
    lines.append("-" * WIDTH)
    lines.append(
        f"run: {result.run_id} | iterations: {result.iterations} "
        f"| {result.latency_s:.1f}s | {FINGERPRINT_LABEL}: {fingerprint or '-'}"
    )
    lines.append("AI-generated, please verify before acting.")
    return "\n".join(lines)


def heading(title: str) -> str:
    """A section title centred in a rule of `=`, in bold so it stands out."""

    return f" {bold(title)} ".center(WIDTH, "=")


def _section(title: str) -> list[str]:
    """Blank lines and a ruled heading, so sections separate at a glance."""

    return ["", "", heading(title), ""]


# Work notes are plain text, so bold comes from Unicode's sans-serif bold letters.
_BOLD = str.maketrans(
    {c: chr(0x1D5D4 + i) for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")}
    | {c: chr(0x1D5EE + i) for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")}
    | {c: chr(0x1D7EC + i) for i, c in enumerate("0123456789")}
)


def bold(text: str) -> str:
    return text.translate(_BOLD)


def _wrap(text: str, *, indent: str = "", first: str | None = None) -> list[str]:
    """Fold prose at WIDTH; the ServiceNow pane does not wrap kindly on its own."""

    if not text:
        return []
    return textwrap.wrap(
        text,
        width=WIDTH,
        initial_indent=indent if first is None else first,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _by_ticket(evidence: Iterable[EvidenceRef]) -> dict[str, list[EvidenceRef]]:
    """One heading and one link per ticket, however many of its fields we quote."""

    grouped: dict[str, list[EvidenceRef]] = {}
    for ref in evidence:
        grouped.setdefault(ref.number, []).append(ref)
    return grouped


def _field_label(ref: EvidenceRef) -> str:
    """The citation's tail, e.g. `close_notes:0`, matching the body's [ticket / field]."""

    _, _, suffix = ref.evidence_id.partition(":")
    return suffix or ref.field


def _flag_lines(flags: list[str], *, indent: str = "") -> list[str]:
    if not flags:
        return []
    return _wrap(
        "!! CHECK BEFORE RUNNING: " + "; ".join(FLAG_LABELS.get(f, f) for f in flags),
        indent=indent,
    )


def _claim_sources(result: RunResult) -> list[tuple[str, list[str]]]:
    """Each cited point, labelled to match its place above, with the evidence behind it.

    Citations live here rather than under each point so the suggestion reads cleanly.
    """

    out: list[tuple[str, list[str]]] = []
    if result.root_cause and result.root_cause.evidence_ids:
        out.append(("Likely Cause", result.root_cause.evidence_ids))
    for index, step in enumerate(result.resolution_steps, start=1):
        if step.evidence_ids:
            out.append((f"Solution {index}", step.evidence_ids))
    cited = [c for c in result.other_claims if c.evidence_ids]
    for index, claim in enumerate(cited, start=1):
        out.append(("Also Worth Knowing" + (f" {index}" if len(cited) > 1 else ""), claim.evidence_ids))
    return out


def _citations(evidence_ids: list[str]) -> str:
    return " ".join(f"[{eid.replace(':', ' / ', 1)}]" for eid in evidence_ids)


def _url(instance_url: str, table: str, sys_id: str) -> str:
    if not instance_url or not sys_id:
        return ""
    return f"{instance_url.rstrip('/')}/nav_to.do?uri={table}.do%3Fsys_id%3D{sys_id}"


def _link(instance_url: str, table: str, sys_id: str) -> str:
    url = _url(instance_url, table, sys_id)
    return f" | {url}" if url else ""


def _date(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "unknown"


def _one_line(text: str) -> str:
    return " ".join(text.split())[:240]
