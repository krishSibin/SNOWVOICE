from __future__ import annotations

from sn_agent.ingest.chunking import chunk_ticket, split_text
from sn_agent.ingest.redact import redact
from sn_agent.ingest.resolution_level import classify_resolution_level
from sn_agent.models import ResolutionLevel

from .factories import make_ticket


def test_redact_removes_contact_details_and_secrets():
    text = "Contact john.doe@corp.com or 555 123 4567. password: hunter2\nServer 10.1.2.3 is down."
    out = redact(text)
    assert "john.doe@corp.com" not in out
    assert "hunter2" not in out
    assert "555 123 4567" not in out
    # IPs stay: they are part of the troubleshooting value.
    assert "10.1.2.3" in out


def test_chunks_carry_source_metadata_and_stable_ids():
    ticket = make_ticket("INC0010001", close_notes="Rebuilt the VPN profile and re-issued the certificate.")
    chunks = chunk_ticket(ticket, ResolutionLevel.RICH)
    ids = [c.evidence_id for c in chunks]

    assert "INC0010001:description:0" in ids
    assert "INC0010001:close_notes:0" in ids
    assert chunk_ticket(ticket, ResolutionLevel.RICH)[0].evidence_id == ids[0]
    for chunk in chunks:
        assert chunk.number == "INC0010001"
        assert chunk.cmdb_ci == "VPN Gateway"
        assert chunk.resolution_level == ResolutionLevel.RICH


def test_split_text_respects_limit():
    parts = split_text("word " * 900, max_chars=500)
    assert parts and all(len(p) <= 500 for p in parts)


def test_resolution_level_rules():
    rich = make_ticket("INC1", close_notes="Replaced the expired certificate on the gateway and restarted the service.")
    weak = make_ticket("INC2", close_notes="Fixed")
    none = make_ticket("INC3", close_notes="")

    assert classify_resolution_level(rich) == ResolutionLevel.RICH
    assert classify_resolution_level(weak) == ResolutionLevel.WEAK
    assert classify_resolution_level(none) == ResolutionLevel.NONE


def test_resolution_level_falls_back_to_weak_without_a_classifier():
    ticket = make_ticket(
        "INC4",
        close_notes="done",
        work_notes=["Checked the gateway logs and found repeated authentication timeouts for remote users."],
    )
    assert classify_resolution_level(ticket) == ResolutionLevel.WEAK
