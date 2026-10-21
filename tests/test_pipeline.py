"""End-to-end pipeline behaviour with a scripted LLM and a fake ServiceNow."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest

from sn_agent.agent.pipeline import ResolutionPipeline
from sn_agent.agent.schemas import (
    ActiveRelatedDraft,
    CitedClaim,
    Diagnostics,
    Outcome,
    QueryPlan,
    ResolutionDraft,
    TicketFacts,
)
from sn_agent.llm import LLMUnavailable
from sn_agent.config import Settings
from sn_agent.notify.formatting import bold, heading, render_note
from sn_agent.sources.servicenow_search import ServiceNowSearchSource

from .factories import NOW, make_ticket
from .fakes import (
    FakeLLM,
    FakeServiceNow,
    evidence_ids_in,
    grade_handler,
    level_handler,
    support_handler,
)

DOCUMENTED = "=== DOCUMENTED RESOLUTIONS"
OPEN_SECTION = "=== OPEN TICKETS"


def settings(**overrides) -> Settings:
    base = dict(agreement_samples=0, max_iterations=3)
    base.update(overrides)
    return Settings(**base)


def draft_handler(*, cite_open: bool = True, self_confidence: float = 0.85):
    def handle(prompt: str) -> ResolutionDraft:
        found = evidence_ids_in(prompt, DOCUMENTED)
        # Prefer the ids a real model would cite for a fix.
        fix_ids = [eid for eid in found if ":close_notes:" in eid] or found
        open_numbers = re.findall(r"\[([A-Z]+\d+):[a-z_]+:\d+\]", prompt[prompt.find(OPEN_SECTION):]) if OPEN_SECTION in prompt else []
        steps = [
            CitedClaim(text="Re-issue the VPN certificate on the gateway", evidence_ids=fix_ids[:1]),
        ] if fix_ids else []
        return ResolutionDraft(
            root_cause=CitedClaim(text="Expired gateway certificate", evidence_ids=fix_ids[:1]) if fix_ids else CitedClaim(text=""),
            resolution_steps=steps,
            active_related=[
                ActiveRelatedDraft(number=number, relation="duplicate", tried_so_far="restarted the gateway, no change")
                for number in dict.fromkeys(open_numbers)
            ] if cite_open else [],
            gaps=["No evidence about the client version"],
            diagnostic_questions=["Which VPN client version is in use?"],
            self_confidence=self_confidence,
        )

    return handle


def build(
    history=(), *, grades: dict, support: str = "supported", open_tickets=(), cfg=None,
    extra_handlers: dict | None = None,
):
    cfg = cfg or settings()
    llm = FakeLLM(
        {
            "intake": lambda p: TicketFacts(summary="VPN error 809 on connect", symptoms=["cannot connect"], error_codes=["809"], affected_component="VPN Gateway", keywords=["vpn", "809", "certificate"]),
            "plan": lambda p: QueryPlan(queries=["vpn error 809", "vpn certificate expired"]),
            "grade": grade_handler(grades),
            "generate_flash": draft_handler(),
            "generate_pro": draft_handler(),
            "resolution_level": level_handler,
            "verify_support": support_handler(support),
            "diagnose": lambda p: Diagnostics(
                gaps=["No log excerpt attached"],
                diagnostic_questions=["Which client version is affected?"],
            ),
            **(extra_handlers or {}),
        }
    )
    instance = FakeServiceNow([*history, *open_tickets])
    source = ServiceNowSearchSource(instance, cfg, llm)
    return ResolutionPipeline(cfg, llm, source), llm, cfg


def test_resolution_found_with_citations():
    history = [
        make_ticket("INC0001", close_notes="The gateway certificate had expired. Re-issued it and restarted the service."),
        make_ticket("INC0002", close_notes="Re-issued the expired VPN certificate on the gateway."),
    ]
    pipeline, llm, cfg = build(
        history,
        grades={"INC0001": ("relevant", 0.9, "unrelated"), "INC0002": ("relevant", 0.85, "unrelated")},
    )

    result = pipeline.run(make_ticket("INC0099", active=True, close_notes=""), include_open=False)

    assert result.outcome == Outcome.RESOLUTION_FOUND
    assert result.resolution_steps
    assert result.confidence.level in {"medium", "high"}
    assert result.evidence and all(ref.evidence_id in {e.evidence_id for e in result.evidence} for ref in result.evidence)
    cited_numbers = {eid.split(":")[0] for step in result.resolution_steps for eid in step.evidence_ids}
    assert cited_numbers <= {"INC0001", "INC0002"}
    note = render_note(result, instance_url="https://example.service-now.com")
    assert heading("SUGGESTED ROOT CAUSE AND RESOLUTION") in note
    assert heading("EVIDENCE USED") in note
    # Citations sit in the evidence section only, not under the suggestion.
    suggestion = note[note.index(heading("SUGGESTED ROOT CAUSE AND RESOLUTION")):note.index(heading("EVIDENCE USED"))]
    assert "[INC000" not in suggestion
    assert bold("Solution 1") in note
    assert "[INC000" in note


def test_similar_tickets_without_recorded_resolution():
    history = [
        make_ticket("INC0003", close_notes="Fixed", resolved_by="A. Kumar"),
        make_ticket("INC0004", close_notes="", resolved_by="B. Silva"),
    ]
    pipeline, llm, cfg = build(
        history,
        grades={"INC0003": ("relevant", 0.9, "unrelated"), "INC0004": ("relevant", 0.8, "unrelated")},
    )

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)

    assert result.outcome == Outcome.SIMILAR_NO_RESOLUTION_RECORDED
    assert result.resolution_steps == []
    assert {s.number for s in result.similar_without_resolution} == {"INC0003", "INC0004"}
    assert any(s.resolved_by == "A. Kumar" for s in result.similar_without_resolution)
    # One wider sweep, then it stops instead of hunting for a fix nobody wrote down.
    assert result.iterations == 2
    assert "generate_flash" not in llm.steps()
    note = render_note(result)
    assert heading("SAME ISSUE BEFORE, NO RESOLUTION RECORDED") in note
    assert "A. Kumar" in note


def test_no_history_at_all():
    pipeline, _llm, _cfg = build(grades={})

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)

    assert result.outcome == Outcome.NO_HISTORY
    assert result.resolution_steps == []
    note = render_note(result)
    assert "No similar past ticket found" in note
    assert heading("SUGGESTED NEXT CHECKS") not in note or result.diagnostic_questions


def test_a_ticket_with_no_documented_fix_still_gets_diagnostic_questions():
    history = [
        make_ticket("INC0003", close_notes="Fixed", resolved_by="A. Kumar"),
    ]
    pipeline, _llm, _cfg = build(history, grades={"INC0003": ("relevant", 0.9, "unrelated")})

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)

    assert result.resolution_steps == []
    assert result.diagnostic_questions == ["Which client version is affected?"]
    assert heading("SUGGESTED NEXT CHECKS") in render_note(result)


def test_model_outage_still_reports_what_was_already_found():
    """An overloaded model must not cost the whole run: the open duplicate is the
    most time-critical part of the note and was found before generation."""

    history = [
        make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway."),
    ]
    open_ticket = make_ticket("INC0050", active=True, days_ago=0, work_notes=["Restarted the gateway, no change."])

    def explode(prompt):
        raise LLMUnavailable("model overloaded")

    pipeline, _llm, _cfg = build(
        history,
        grades={"INC0001": ("relevant", 0.9, "unrelated"), "INC0050": ("relevant", 0.9, "duplicate")},
        open_tickets=[open_ticket],
        extra_handlers={"generate_flash": explode, "generate_pro": explode},
    )

    result = pipeline.run(make_ticket("INC0099", active=True))

    assert result.stopped_early == "the language model was unavailable"
    assert [a.number for a in result.active_related] == ["INC0050"]
    assert result.resolution_steps == []
    assert "stopped early" in render_note(result)


def _fake_response(text: str):
    class Message:
        content = text

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = None

    return Response()


def _api_status_error(cls, status_code: int, message: str):
    import httpx

    request = httpx.Request("POST", "https://genailab.tcs.in/chat/completions")
    response = httpx.Response(status_code, request=request)
    return cls(message, response=response, body=None)


def test_a_retired_model_falls_back_instead_of_failing():
    """A model can be listed by the gateway but rejected for a given key (404).
    That must move to the next model, not end the ticket."""

    import openai

    from sn_agent.config import Settings
    from sn_agent.llm import Meter, TcsGenAiLLM, Tier

    calls: list[str] = []

    class Completions:
        def create(self, *, model, messages, response_format):
            calls.append(model)
            if model == "retired-pro":
                raise _api_status_error(openai.NotFoundError, 404, "no longer available")
            return _fake_response('{"queries": ["vpn 809"]}')

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    cfg = Settings(model_pro="retired-pro", model_flash="working-flash", model_lite="working-lite")
    llm = TcsGenAiLLM(cfg, client=Client())

    plan = llm.generate_json(
        tier=Tier.PRO, system="s", prompt="p", schema=QueryPlan,
        meter=Meter(cfg.prices, 10.0, 10**6), step="plan",
    )

    assert plan.queries == ["vpn 809"]
    assert calls == ["retired-pro", "working-flash"]
    # The dead model is remembered, so the next call skips it.
    assert "retired-pro" not in llm.fallback_models(Tier.PRO)


def test_a_rate_limited_model_is_skipped_immediately_and_rested():
    """A 429 is a per-minute quota: waiting seconds and retrying the same model
    wastes time, so fall back at once and leave that model alone for a while."""

    import openai

    from sn_agent.config import Settings
    from sn_agent.llm import Meter, TcsGenAiLLM, Tier

    calls: list[str] = []

    class Completions:
        def create(self, *, model, messages, response_format):
            calls.append(model)
            if model == "busy-flash":
                raise _api_status_error(openai.RateLimitError, 429, "quota")
            return _fake_response('{"queries": ["vpn 809"]}')

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    cfg = Settings(model_flash="busy-flash", model_lite="spare-lite", llm_max_retries=3)
    llm = TcsGenAiLLM(cfg, client=Client())
    meter = Meter(cfg.prices, 10.0, 10**6)

    llm.generate_json(tier=Tier.FLASH, system="s", prompt="p", schema=QueryPlan, meter=meter, step="plan")
    llm.generate_json(tier=Tier.FLASH, system="s", prompt="p", schema=QueryPlan, meter=meter, step="plan")

    # One attempt on the limited model, no retries, and not tried again while resting.
    assert calls == ["busy-flash", "spare-lite", "spare-lite"]


def test_open_duplicate_is_reported_with_what_was_already_tried():
    history = [
        make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway."),
    ]
    open_ticket = make_ticket(
        "INC0050", active=True, days_ago=0, assigned_to="R. Mehta",
        work_notes=["Restarted the VPN gateway service but users still get error 809."],
    )
    pipeline, llm, cfg = build(
        history,
        grades={
            "INC0001": ("relevant", 0.9, "unrelated"),
            "INC0050": ("relevant", 0.9, "duplicate"),
        },
        open_tickets=[open_ticket],
    )

    result = pipeline.run(make_ticket("INC0099", active=True))

    assert [a.number for a in result.active_related] == ["INC0050"]
    assert result.active_related[0].assigned_to == "R. Mehta"
    assert result.active_related[0].tried_so_far
    assert result.active_issue_in_progress is True
    note = render_note(result)
    assert heading("ACTIVE RELATED TICKETS") in note
    if heading("SUGGESTED ROOT CAUSE AND RESOLUTION") in note:
        assert note.index(heading("SUGGESTED ROOT CAUSE AND RESOLUTION")) < note.index(heading("ACTIVE RELATED TICKETS"))


def test_several_open_duplicates_raise_a_major_incident_warning():
    open_tickets = [
        make_ticket(f"INC005{i}", active=True, days_ago=0, work_notes=["Same VPN 809 error reported."])
        for i in range(2)
    ]
    for ticket in open_tickets:
        ticket.opened_at = NOW - timedelta(hours=2)
    pipeline, _llm, _cfg = build(
        grades={t.number: ("relevant", 0.9, "duplicate") for t in open_tickets},
        open_tickets=open_tickets,
    )

    result = pipeline.run(make_ticket("INC0099", active=True))

    assert result.major_incident_warning is True
    assert "major incident" in render_note(result)


def test_low_confidence_loops_escalates_then_gives_up_without_inventing_a_fix():
    history = [
        make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway."),
    ]
    pipeline, llm, cfg = build(
        history,
        grades={"INC0001": ("partial", 0.5, "unrelated")},
        support="unsupported",
    )

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)

    assert result.iterations == cfg.max_iterations
    assert "generate_pro" in llm.steps()  # escalated after the first low score
    assert result.resolution_steps == []
    assert result.outcome == Outcome.INSUFFICIENT_EVIDENCE
    assert result.confidence.level == "low"


def test_budget_cap_stops_the_run_cleanly():
    history = [
        make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway."),
    ]
    pipeline, _llm, _cfg = build(
        history,
        grades={"INC0001": ("relevant", 0.9, "unrelated")},
        cfg=settings(max_tokens_per_ticket=200),
    )

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)

    assert result.budget_exhausted is True
    assert result.outcome in {Outcome.NO_HISTORY, Outcome.INSUFFICIENT_EVIDENCE}
    assert result.resolution_steps == []


@pytest.mark.parametrize("support", ["supported", "partial"])
def test_support_label_is_carried_into_the_note(support):
    history = [
        make_ticket("INC0001", close_notes="Re-issued the expired VPN certificate on the gateway."),
    ]
    pipeline, _llm, _cfg = build(history, grades={"INC0001": ("relevant", 0.9, "unrelated")}, support=support)

    result = pipeline.run(make_ticket("INC0099", active=True), include_open=False)
    note = render_note(result)

    if support == "partial":
        assert "partly supported" in note
    else:
        assert "partly supported" not in note


def test_waits_for_the_quota_when_every_model_is_rate_limited():
    """Every model at its rate limit at once: wait for the quota, don't give up."""

    import openai

    from sn_agent.config import Settings
    from sn_agent.llm import Meter, TcsGenAiLLM, Tier

    calls: list[str] = []

    class Completions:
        def create(self, *, model, **_kw):
            calls.append(model)
            if len(calls) <= 2:
                raise _api_status_error(openai.RateLimitError, 429, "quota")
            return _fake_response('{"queries": ["vpn 809"]}')

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    cfg = Settings(model_flash="flash", model_lite="lite", llm_max_retries=3)
    llm = TcsGenAiLLM(cfg, client=Client())
    waits: list[float] = []

    def wait(seconds):
        waits.append(seconds)
        llm._cooling_until.clear()

    llm._wait = wait
    meter = Meter(cfg.prices, 10.0, 10**6)

    llm.generate_json(tier=Tier.FLASH, system="s", prompt="p", schema=QueryPlan, meter=meter, step="plan")

    assert calls == ["flash", "lite", "flash"]
    assert len(waits) == 1 and 0 < waits[0] <= 61

