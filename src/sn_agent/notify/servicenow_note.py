from __future__ import annotations

import logging

from ..agent.schemas import RunResult
from ..servicenow.client import ServiceNowClient
from .base import PublishResult
from .formatting import render_note

log = logging.getLogger(__name__)


class ServiceNowWorkNoteNotifier:
    """Appends a work note. It touches no other field, and the integration user
    should not have rights to any."""

    channel = "servicenow_work_note"

    def __init__(self, client: ServiceNowClient, *, dry_run: bool = False):
        self.client = client
        self.dry_run = dry_run

    def publish(
        self, result: RunResult, *, previous_run_id: str = "", fingerprint: str = ""
    ) -> PublishResult:
        note = render_note(
            result,
            instance_url=self.client.base_url,
            previous_run_id=previous_run_id,
            fingerprint=fingerprint,
        )
        if self.dry_run:
            log.info("dry run: the note was not posted to ServiceNow")
            return PublishResult(channel=self.channel, delivered=False, detail="dry-run", body=note)
        log.info("posting the work note to ServiceNow")
        self.client.post_work_note(result.table, result.ticket_sys_id, note)
        log.info("work note posted (run %s)", result.run_id)
        return PublishResult(channel=self.channel, delivered=True, detail="posted", body=note)
