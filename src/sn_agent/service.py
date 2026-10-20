"""Wires the pieces together for one ticket.

Nothing is stored outside ServiceNow, so the ticket's own work notes are what
tell us whether the agent has already answered it. The note carries a fingerprint
of the text it was answering; if the ticket has since been rewritten, we answer
again and point back at the previous note.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from .agent.pipeline import ResolutionPipeline
from .agent.schemas import RunResult
from .config import Settings
from .models import Ticket
from .notify.base import PublishResult
from .notify.formatting import find_previous_note
from .observability import ticket_context
from .servicenow.client import ServiceNowClient

log = logging.getLogger(__name__)


def fingerprint(ticket: Ticket) -> str:
    return hashlib.sha256(ticket.matching_text().encode("utf-8")).hexdigest()[:12]


@dataclass
class ResolveOutcome:
    result: RunResult | None
    published: PublishResult | None
    skipped_reason: str = ""


class ResolutionService:
    def __init__(
        self,
        settings: Settings,
        *,
        client: ServiceNowClient,
        pipeline: ResolutionPipeline,
        notifier,
    ):
        self.settings = settings
        self.client = client
        self.pipeline = pipeline
        self.notifier = notifier

    def resolve(
        self, number: str, table: str = "incident", *, force: bool = False, ticket: Ticket | None = None
    ) -> ResolveOutcome:
        with ticket_context(number):
            log.info("fetching the ticket from ServiceNow")
            ticket = ticket or self.client.get_ticket(number, table)
            log.info(
                'fetched: "%s" | %s | %s | %d work note(s)',
                ticket.short_description,
                ticket.state_label or "state unknown",
                ticket.priority or "priority unknown",
                len(ticket.work_notes),
            )
            digest = fingerprint(ticket)
            previous = find_previous_note(ticket.journal)

            if previous and previous.fingerprint == digest and not force:
                reason = f"already answered in run {previous.run_id}"
                log.info("skipping: %s, and the ticket has not changed since", reason)
                return ResolveOutcome(None, None, skipped_reason=reason)
            if previous:
                log.info("the ticket changed since the last note (run %s); answering again", previous.run_id)

            result = self.pipeline.run(ticket)
            published = self.notifier.publish(
                result,
                previous_run_id=previous.run_id if previous else "",
                fingerprint=digest,
            )
            return ResolveOutcome(result, published)
