"""The local ticket index: what sync stores, what search finds, and that the
pipeline runs on it without a model call per candidate."""

from __future__ import annotations

from sn_agent.agent.pipeline import ResolutionPipeline
from sn_agent.agent.schemas import Outcome
from sn_agent.config import Settings
from sn_agent.intake.evidence import HumanNotesOnly
from sn_agent.llm import Meter
from sn_agent.models import ResolutionLevel, SearchScope
from sn_agent.notify.formatting import HEADER as AGENT_NOTE_HEADER
from sn_agent.sources.servicenow_search import ServiceNowSearchSource
from sn_agent.sources.ticket_index import IndexSearchSource, TicketIndex, fts_query, knowledge_source, sync_index

from .factories import make_ticket
from .fakes import FakeLLM, FakeServiceNow, grade_handler, level_handler
from .test_pipeline import build


def settings(**overrides) -> Settings:
    return Settings(**{"agreement_samples": 0, "max_iterations": 3, **overrides})


def meter(cfg: Settings) -> Meter:
    return Meter(cfg.prices, 10.0, 10**6)


def indexed(*tickets, level=ResolutionLevel.RICH) -> TicketIndex:
    index = TicketIndex(":memory:")
    for ticket in tickets:
        index.upsert(ticket, level)
    return index


def test_any_word_matches_and_title_words_rank_first():
    index = indexed(
        make_ticket("INC0001", short_description="Printer jams on floor 3", description="Paper stuck", category="hardware"),
        make_ticket("INC0002", short_description="VPN error 809 from home", description="Cannot connect"),
        make_ticket("INC0003", short_description="Outlook slow", description="Mail mentions the vpn once"),
    )

    keys = index.search_text("my laptop can't reach the VPN")

    assert keys[0] == "incident:INC0002"
    assert "incident:INC0001" not in keys


def test_query_text_cannot_break_the_search_syntax():
    index = indexed(make_ticket("INC0001", short_description="Error 0x80070005 in C:\\Windows\\Temp"))

    assert fts_query('"quoted" (and) OR * NEAR') == '"quoted" OR "near"'
    assert index.search_text('error "0x80070005" * (temp)') == ["incident:INC0001"]
    assert index.search_text("the and of") == []


def test_stored_copy_is_redacted():
    index = indexed(
        make_ticket(
            "INC0001",
            description="Call me on +44 7700 900123 or mail ana@company.com",
            close_notes="Reset password: hunter2 for the user",
        )
    )

    ticket, _level = index.load(["incident:INC0001"])["incident:INC0001"]

    assert "ana@company.com" not in ticket.description and "[EMAIL]" in ticket.description
    assert "hunter2" not in ticket.close_notes
    assert index.search_text("ana company") == []


def test_source_finds_candidates_without_any_model_call():
    cfg = settings()
    index = indexed(
        make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway."),
        make_ticket("INC0002", short_description="Printer offline", description="Toner", category="hardware",
                    cmdb_ci="Printer 7", business_service="Printing"),
    )
    source = IndexSearchSource(index, cfg)

    found = source.search(
        ticket=make_ticket("INC0099", active=True), queries=["vpn 809"], scope=SearchScope(), limit=10, meter=meter(cfg)
    )

    assert [c.number for c in found] == ["INC0001"]
    assert found[0].resolution_level == ResolutionLevel.RICH  # decided at sync time


def test_the_ticket_being_answered_is_never_its_own_evidence():
    cfg = settings()
    index = indexed(make_ticket("INC0099"))
    source = IndexSearchSource(index, cfg)

    found = source.search(
        ticket=make_ticket("INC0099", active=True), queries=["vpn"], scope=SearchScope(), limit=10, meter=meter(cfg)
    )

    assert found == []


def test_hybrid_falls_back_to_live_only_when_the_index_has_nothing():
    cfg = settings()
    live = ServiceNowSearchSource(
        FakeServiceNow([make_ticket("INC0005", close_notes="Re-issued the VPN certificate.")]),
        cfg,
        FakeLLM({"resolution_level": level_handler}),
    )
    empty = IndexSearchSource(TicketIndex(":memory:"), cfg, live, fall_back_to_live=True)
    full = IndexSearchSource(indexed(make_ticket("INC0001")), cfg, live, fall_back_to_live=True)
    args = dict(ticket=make_ticket("INC0099", active=True), queries=["vpn"], scope=SearchScope(), limit=10)

    assert [c.number for c in empty.search(**args, meter=meter(cfg))] == ["INC0005"]
    assert [c.number for c in full.search(**args, meter=meter(cfg))] == ["INC0001"]


def test_open_tickets_are_still_found_live():
    cfg = settings()
    live = ServiceNowSearchSource(FakeServiceNow([make_ticket("INC0050", active=True)]), cfg, FakeLLM())
    source = IndexSearchSource(TicketIndex(":memory:"), cfg, live)

    found = source.find_open(make_ticket("INC0099", active=True), ["vpn 809"], meter(cfg))

    assert [c.number for c in found] == ["INC0050"]


def test_live_stays_the_default():
    live = object()

    assert knowledge_source(settings(), live) is live
    assert isinstance(knowledge_source(settings(knowledge_source="index", app_db_path=":memory:"), live), IndexSearchSource)


def test_pipeline_answers_from_the_index():
    history = [make_ticket("INC0001", close_notes="The gateway certificate had expired. Re-issued it and restarted the service.")]
    pipeline, llm, cfg = build(history, grades={"INC0001": ("relevant", 0.9, "unrelated")})
    pipeline.source = IndexSearchSource(indexed(*history), cfg, pipeline.source)

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)

    assert result.outcome == Outcome.RESOLUTION_FOUND
    assert "resolution_level" not in llm.steps()


def test_sync_stores_resolved_tickets_without_the_agents_own_notes():
    cfg = settings()
    agent_note = f"{AGENT_NOTE_HEADER}\nTry re-issuing the certificate."
    instance = FakeServiceNow(
        [
            make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway.",
                        work_notes=[agent_note, "Checked the gateway logs: certificate expired yesterday."]),
            make_ticket("INC0002", active=True),  # reopened: must not stay in the index
        ]
    )
    index = indexed(make_ticket("INC0002"))
    llm = FakeLLM({"resolution_level": level_handler})

    report = sync_index(index, HumanNotesOnly(instance), cfg, llm)

    assert report.stored == 1 and report.removed == 1
    ticket, level = index.load(["incident:INC0001"])["incident:INC0001"]
    assert level == ResolutionLevel.RICH
    assert all(AGENT_NOTE_HEADER not in note.value for note in ticket.journal)
    assert index.load(["incident:INC0002"]) == {}
    assert index.get_meta("synced:incident")


def test_a_later_sync_only_asks_for_what_changed():
    cfg = settings()
    instance = FakeServiceNow([make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate.")])
    index = TicketIndex(":memory:")

    sync_index(index, HumanNotesOnly(instance), cfg, None)
    first = [q for t, q in instance.queries if t == "incident"][-1]
    sync_index(index, HumanNotesOnly(instance), cfg, None)
    second = [q for t, q in instance.queries if t == "incident"][-1]

    assert "daysAgoStart" in first and "sys_updated_on>=" not in first
    assert "sys_updated_on>=" in second


def test_unclear_tickets_are_rechecked_when_the_model_was_unavailable():
    from sn_agent.llm import LLMUnavailable

    def down(_prompt):
        raise LLMUnavailable("quota")

    cfg = settings()
    unclear = make_ticket("INC0001", close_notes="Restarted it.", work_notes=["Spoke to the user, cleared the cache and the client profile."])
    instance = FakeServiceNow([unclear])
    index = TicketIndex(":memory:")

    first = sync_index(index, HumanNotesOnly(instance), cfg, FakeLLM({"resolution_level": down}))
    instance.tickets.clear()  # nothing changed in ServiceNow since
    second = sync_index(index, HumanNotesOnly(instance), cfg, FakeLLM({"resolution_level": level_handler}))

    assert first.unchecked == 1
    assert second.rechecked == 1 and index.stats()["unchecked"] == 0
