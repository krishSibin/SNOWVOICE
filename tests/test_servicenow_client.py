"""The REST client against a mocked instance: pagination, retries, and the fact
that the only write is a work note."""

from __future__ import annotations

import json

import httpx
import pytest

from sn_agent.config import Settings
from sn_agent.servicenow.client import ServiceNowClient, ServiceNowError, parse_journal

INSTANCE = "https://example.service-now.com"


def client_with(handler) -> ServiceNowClient:
    settings = Settings(
        servicenow_instance_url=INSTANCE,
        servicenow_auth_mode="basic",
        servicenow_username="svc",
        servicenow_password="secret",
        servicenow_min_request_interval_s=0.0,
    )
    transport = httpx.MockTransport(handler)
    http = httpx.Client(base_url=INSTANCE, transport=transport)
    return ServiceNowClient(settings, client=http)


def record(number: str, **fields) -> dict:
    base = {
        "sys_id": {"value": f"sys-{number}", "display_value": f"sys-{number}"},
        "number": {"value": number, "display_value": number},
        "short_description": {"value": "VPN fails", "display_value": "VPN fails"},
        "description": {"value": "error 809", "display_value": "error 809"},
        "close_notes": {"value": "Re-issued certificate", "display_value": "Re-issued certificate"},
        "state": {"value": "6", "display_value": "Resolved"},
        "active": {"value": "false", "display_value": "false"},
        "cmdb_ci": {"value": "ci-1", "display_value": "VPN Gateway"},
        "assignment_group": {"value": "grp-1", "display_value": "Network Support"},
        "resolved_at": {"value": "2026-09-01 10:00:00", "display_value": "2026-09-01 10:00:00"},
        "sys_updated_on": {"value": "2026-09-01 11:00:00", "display_value": "2026-09-01 11:00:00"},
        "reopen_count": {"value": "1", "display_value": "1"},
    }
    base.update({k: {"value": v, "display_value": v} for k, v in fields.items()})
    return base


def test_records_are_mapped_with_values_and_display_values():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [record("INC0001")]})

    ticket = client_with(handler).get_ticket("INC0001", with_journal=False)

    assert ticket.number == "INC0001"
    assert ticket.cmdb_ci == "VPN Gateway"  # display value
    assert ticket.cmdb_ci_id == "ci-1"  # raw value, used for filtering
    assert ticket.reopen_count == 1
    assert ticket.resolved_at is not None and ticket.resolved_at.year == 2026


def test_resolved_incident_is_not_treated_as_open_even_when_active_is_true():
    """Many instances leave `active` true on a resolved incident until it
    auto-closes. Trusting that flag would report closed tickets as live duplicates."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [record("INC0001", active="true")]})

    ticket = client_with(handler).get_ticket("INC0001", with_journal=False)

    assert ticket.resolved_at is not None
    assert ticket.active is False


def test_open_incident_is_still_open():
    def handler(request: httpx.Request) -> httpx.Response:
        row = record("INC0002", active="true", state="2")
        row["resolved_at"] = {"value": "", "display_value": ""}
        return httpx.Response(200, json={"result": [row]})

    ticket = client_with(handler).get_ticket("INC0002", with_journal=False)

    assert ticket.active is True


def test_problem_records_are_knowledge_never_live_duplicates():
    def handler(request: httpx.Request) -> httpx.Response:
        row = record("PRB0001", active="true", state="101")
        row["resolved_at"] = {"value": "", "display_value": ""}
        return httpx.Response(200, json={"result": [row]})

    ticket = client_with(handler).get_ticket("PRB0001", table="problem", with_journal=False)

    assert ticket.active is False


def test_pagination_walks_every_page():
    pages = [[record(f"INC000{i}") for i in range(2)], [record("INC0009")], []]
    seen_offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("sysparm_offset", 0))
        seen_offsets.append(offset)
        page = pages.pop(0) if pages else []
        return httpx.Response(200, json={"result": page})

    settings_client = client_with(handler)
    settings_client.settings.servicenow_page_size = 2
    rows = list(settings_client.query_records("incident", "active=false"))

    assert len(rows) == 3
    assert seen_offsets[:2] == [0, 2]


def test_rate_limit_is_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"result": [record("INC0001")]})

    rows = list(client_with(handler).query_records("incident", "active=false", limit=1))

    assert calls["n"] == 2
    assert rows


def test_server_error_is_raised_after_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    sn = client_with(handler)
    sn.settings.servicenow_max_retries = 1
    with pytest.raises(ServiceNowError):
        list(sn.query_records("incident", "active=false"))


def test_work_note_write_touches_no_other_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": {"sys_id": "sys-1"}})

    client_with(handler).post_work_note("incident", "sys-1", "hello")

    assert captured["method"] == "PATCH"
    assert "/api/now/table/incident/sys-1" in captured["url"]
    assert captured["body"] == {"work_notes": "hello"}


def test_journal_is_parsed_from_the_ticket_field():
    """Notes are read through the ticket's own field, which any account that can
    read the ticket can see; the journal table itself is admin-only."""

    text = (
        "2026-09-17 11:34:56 - System Administrator (Work notes)\n"
        "Gateway log: certificate verify failed.\n\n"
        "2026-09-17 10:00:00 - Jane Doe (Work notes)\n"
        "Reproduced with the test account.\nSecond line of the same note.\n\n"
    )

    entries = parse_journal(text, "work_notes")

    assert [e.value for e in entries] == [
        "Gateway log: certificate verify failed.",
        "Reproduced with the test account.\nSecond line of the same note.",
    ]
    assert [e.created_by for e in entries] == ["System Administrator", "Jane Doe"]
    assert all(e.element == "work_notes" for e in entries)
    assert entries[1].created_on.hour == 10


def test_get_journal_reads_notes_from_the_ticket_not_the_journal_table():
    captured = {}
    notes = "2026-09-17 11:34:56 - Admin (Work notes)\nRestarted the service.\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["query"] = request.url.params.get("sysparm_query")
        return httpx.Response(200, json={"result": [record("INC0001", work_notes=notes)]})

    journal = client_with(handler).get_journal("incident", ["sys-INC0001"])

    assert captured["path"] == "/api/now/table/incident"
    assert captured["query"] == "sys_idINsys-INC0001"
    assert [e.value for e in journal["sys-INC0001"]] == ["Restarted the service."]


def test_full_text_search_uses_the_instance_search_engine():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params.get("sysparm_query")
        return httpx.Response(200, json={"result": [record("INC0001")]})

    rows = client_with(handler).search_text(
        "incident", "vpn certificate expired", clauses=["resolved_atISNOTEMPTY"], limit=5
    )

    assert captured["query"].startswith("123TEXTQUERY321=vpn certificate expired")
    assert "^resolved_atISNOTEMPTY" in captured["query"]
    assert len(rows) == 1


def test_search_text_cannot_be_broken_by_query_characters():
    """`^` and `=` separate conditions in an encoded query, so unsanitised search
    text could rewrite the query it is embedded in."""

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params.get("sysparm_query")
        return httpx.Response(200, json={"result": []})

    client_with(handler).search_text(
        "incident", "vpn^active=true^ORDERBYnumber", clauses=["resolved_atISNOTEMPTY"]
    )

    assert captured["query"] == "123TEXTQUERY321=vpn active true ORDERBYnumber^resolved_atISNOTEMPTY"


def test_scoped_sweep_queries_by_field():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = request.url.params.get("sysparm_query")
        return httpx.Response(200, json={"result": []})

    client_with(handler).search_by_fields(
        "incident", {"cmdb_ci": "ci-1"}, clauses=["resolved_atISNOTEMPTY"], limit=10
    )

    assert captured["query"] == "cmdb_ci=ci-1^resolved_atISNOTEMPTY^ORDERBYDESCopened_at"


