"""The service layer, whose only memory of a ticket is the ticket itself."""

from __future__ import annotations

import hashlib
import hmac

from fastapi.testclient import TestClient

from sn_agent.agent.schemas import Outcome, RunResult
from sn_agent.config import Settings
from sn_agent.notify.servicenow_note import ServiceNowWorkNoteNotifier
from sn_agent.service import ResolutionService

from .factories import make_ticket
from .fakes import FakeServiceNow


class StubPipeline:
    def __init__(self) -> None:
        self.runs = 0

    def run(self, ticket, **kwargs) -> RunResult:
        self.runs += 1
        return RunResult(
            run_id=f"run-0000000{self.runs}",
            ticket_number=ticket.number,
            ticket_sys_id=ticket.sys_id,
            table=ticket.table,
            outcome=Outcome.NO_HISTORY,
        )


def build_service(ticket, *, dry_run: bool = False):
    instance = FakeServiceNow([ticket])
    pipeline = StubPipeline()
    service = ResolutionService(
        Settings(),
        client=instance,
        pipeline=pipeline,
        notifier=ServiceNowWorkNoteNotifier(instance, dry_run=dry_run),
    )
    return service, instance, pipeline


def test_note_is_posted_to_the_ticket():
    ticket = make_ticket("INC0100", active=True)
    service, instance, _pipeline = build_service(ticket)

    outcome = service.resolve("INC0100")

    assert outcome.published.delivered is True
    assert instance.posted[0][0:2] == ("incident", ticket.sys_id)


def test_unchanged_ticket_is_not_answered_twice():
    """The posted note carries a fingerprint of the ticket text, so the ticket
    itself tells us we have already answered it."""

    ticket = make_ticket("INC0100", active=True)
    service, instance, pipeline = build_service(ticket)

    service.resolve("INC0100")
    second = service.resolve("INC0100")

    assert second.skipped_reason.startswith("already answered")
    assert pipeline.runs == 1
    assert len(instance.posted) == 1


def test_force_reruns_and_links_back_to_the_previous_note():
    ticket = make_ticket("INC0100", active=True)
    service, instance, pipeline = build_service(ticket)

    service.resolve("INC0100")
    service.resolve("INC0100", force=True)

    assert pipeline.runs == 2
    assert "replaces an earlier note from run run-00000001" in instance.posted[1][2]


def test_rewritten_ticket_is_answered_again():
    ticket = make_ticket("INC0100", active=True)
    service, _instance, pipeline = build_service(ticket)
    service.resolve("INC0100")

    ticket.short_description = "Printer in Building C jams on duplex printing"
    ticket.description = "Paper jam every time duplex printing is used on the third floor."
    service.resolve("INC0100", ticket=ticket)

    assert pipeline.runs == 2


def test_dry_run_does_not_write_to_servicenow():
    ticket = make_ticket("INC0100", active=True)
    service, instance, _pipeline = build_service(ticket, dry_run=True)

    outcome = service.resolve("INC0100")

    assert outcome.published.delivered is False
    assert instance.posted == []
    assert "[AI Resolution Assistant]" in outcome.published.body


def test_webhook_defaults_to_off():
    assert Settings(_env_file=None).webhook_enabled is False


def _api_client(monkeypatch):
    from sn_agent.api import app as api_module
    from sn_agent.config import get_settings
    from sn_agent.runner import Submit

    get_settings.cache_clear()
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "topsecret")
    monkeypatch.setenv("SWEEP_ENABLED", "false")
    submitted: list[tuple[str, str, int]] = []

    def record(number, table, priority):
        submitted.append((number, table, priority))
        return Submit.QUEUED

    monkeypatch.setattr(api_module.runner, "submit", record)
    return TestClient(api_module.app), submitted


def test_webhook_is_off_by_default_and_looks_like_it_does_not_exist(monkeypatch):
    client, submitted = _api_client(monkeypatch)
    monkeypatch.setenv("WEBHOOK_ENABLED", "false")
    from sn_agent.config import get_settings

    get_settings.cache_clear()
    body = b'{"number": "INC0100"}'
    signature = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    response = client.post("/webhook/servicenow", content=body, headers={"X-SN-Signature": signature})

    assert response.status_code == 404
    assert submitted == []


def test_webhook_rejects_a_bad_signature(monkeypatch):
    client, submitted = _api_client(monkeypatch)

    response = client.post(
        "/webhook/servicenow", content=b'{"number": "INC1"}', headers={"X-SN-Signature": "deadbeef"}
    )

    assert response.status_code == 401
    assert submitted == []


def test_webhook_queues_the_ticket_at_its_priority(monkeypatch):
    client, submitted = _api_client(monkeypatch)
    body = b'{"number": "INC0100", "table": "incident", "priority": "1", "event": "created"}'
    signature = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    response = client.post("/webhook/servicenow", content=body, headers={"X-SN-Signature": signature})

    assert response.status_code == 202
    assert submitted == [("INC0100", "incident", 1)]


def test_webhook_without_a_readable_priority_queues_it_as_middling(monkeypatch):
    client, submitted = _api_client(monkeypatch)
    body = b'{"number": "INC0101", "priority": "urgent!!"}'
    signature = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()

    client.post("/webhook/servicenow", content=body, headers={"X-SN-Signature": signature})

    assert submitted == [("INC0101", "incident", 3)]
