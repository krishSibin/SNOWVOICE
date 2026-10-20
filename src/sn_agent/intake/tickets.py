"""Raising the incident.

This is the only place the intake agent writes to ServiceNow, and it does so
through its own account (SN_INTAKE_USERNAME), never the ticket agent's. With
INTAKE_CREATE_TICKETS off it builds the exact payload and creates nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..servicenow.client import ServiceNowClient, ServiceNowError

log = logging.getLogger(__name__)


@dataclass
class IncidentDraft:
    short_description: str
    description: str
    impact: int
    urgency: int
    expected_priority: int
    assignment_group: str
    assignment_group_sys_id: str = ""
    category: str = ""
    caller_email: str = ""
    work_note: str = ""
    channel: str = "text"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CreatedIncident:
    number: str
    sys_id: str
    created: bool  # False on a dry run
    payload: dict[str, Any]
    url: str = ""


def intake_client(settings: Settings) -> ServiceNowClient | None:
    """A client logged in as the intake account, or None when none is set."""

    if not (settings.sn_intake_username and settings.sn_intake_password.get_secret_value()):
        return None
    return ServiceNowClient(
        settings.model_copy(
            update={
                "servicenow_auth_mode": "basic",
                "servicenow_username": settings.sn_intake_username,
                "servicenow_password": settings.sn_intake_password,
            }
        )
    )


class IncidentWriter:
    def __init__(self, settings: Settings, client: Any | None):
        self.settings = settings
        self.client = client

    @property
    def live(self) -> bool:
        return bool(self.settings.intake_create_tickets and self.client is not None)

    def payload(self, draft: IncidentDraft, caller_sys_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {
            "short_description": draft.short_description[:160],
            "description": draft.description,
            "impact": str(draft.impact),
            "urgency": str(draft.urgency),
            "contact_type": "virtual_agent",
            "correlation_id": self.settings.intake_correlation_id,
        }
        if draft.category:
            body["category"] = draft.category
        if draft.assignment_group_sys_id:
            body["assignment_group"] = draft.assignment_group_sys_id
        elif draft.assignment_group:
            # Accepted by the Table API as a display value lookup.
            body["assignment_group"] = draft.assignment_group
        if caller_sys_id:
            body["caller_id"] = caller_sys_id
        return body

    def find_caller(self, email: str) -> str:
        if not (email and self.client is not None):
            return ""
        try:
            rows = list(
                self.client.query_records(
                    "sys_user", f"email={email}^active=true", fields=["sys_id"], limit=1
                )
            )
        except ServiceNowError as exc:
            log.warning("could not look up the caller %s: %s", email, exc)
            return ""
        if not rows:
            return ""
        raw = rows[0].get("sys_id")
        return str(raw.get("value") if isinstance(raw, dict) else raw or "")

    def create(self, draft: IncidentDraft) -> CreatedIncident:
        caller = self.find_caller(draft.caller_email) if self.live else ""
        body = self.payload(draft, caller)
        if not self.live:
            reason = "INTAKE_CREATE_TICKETS is off" if self.client is not None else "no intake account is set"
            log.info("dry run (%s): would create incident %s", reason, body)
            return CreatedIncident(number="", sys_id="", created=False, payload=body)

        response = self.client._request(  # noqa: SLF001 - same retry and error handling as every other call
            "POST",
            "/api/now/table/incident",
            params={"sysparm_fields": "sys_id,number", "sysparm_display_value": "false"},
            json=body,
            headers={"Content-Type": "application/json"},
        )
        result = response.json().get("result", {})
        number, sys_id = str(result.get("number", "")), str(result.get("sys_id", ""))
        log.info("created %s (%s) for %s", number, sys_id, draft.caller_email or "unknown caller")
        if draft.work_note and sys_id:
            try:
                self.client.post_work_note("incident", sys_id, draft.work_note)
            except ServiceNowError as exc:
                log.warning("incident %s created, but its work note failed: %s", number, exc)
        url = f"{self.client.base_url}/nav_to.do?uri=incident.do%3Fsys_id%3D{sys_id}" if sys_id else ""
        return CreatedIncident(number=number, sys_id=sys_id, created=True, payload=body, url=url)
