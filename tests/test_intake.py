"""The intake agent: finding a documented fix in conversation, the rules for
priority and team, and raising (or, by default, only drafting) the incident."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from sn_agent.config import Settings
from sn_agent.intake.chat import SEARCH_FILLER, TextChat
from sn_agent.intake.evidence import FixFinder, HumanNotesOnly, IssueReport, ParallelSearchSource, UserFixDraft, UserStep
from sn_agent.intake.routing import GroupChooser, GroupDirectory, decide_priority
from sn_agent.intake.session import IntakeServices, IntakeSession
from sn_agent.intake.tickets import IncidentWriter
from sn_agent.models import JournalEntry
from sn_agent.notify.formatting import HEADER
from sn_agent.store import Store

from .factories import make_ticket
from .fakes import FakeLLM, FakeServiceNow, evidence_ids_in, grade_handler, level_handler, support_handler

DOCUMENTED = "=== DOCUMENTED RESOLUTIONS"
VPN_FIX = "Re-issued the expired VPN certificate on the gateway and restarted the IKE service."


def settings(**overrides) -> Settings:
    base = dict(intake_default_group="Service Desk", tcs_genai_api_key="x")
    base.update(overrides)
    return Settings(**base)


def fix_handler(*, technician: bool = False):
    def handle(prompt: str) -> UserFixDraft:
        ids = [eid for eid in evidence_ids_in(prompt, DOCUMENTED) if ":close_notes:" in eid or ":work_notes:" in eid]
        steps = [UserStep(text="Reconnect after the certificate is re-issued", evidence_ids=ids[:1], needs_technician=technician)]
        return UserFixDraft(cause="Expired gateway certificate", steps=steps if ids else [])

    return handle


def build(history=(), *, grades: dict, support: str = "supported", technician: bool = False, cfg=None, open_tickets=()):
    cfg = cfg or settings()
    llm = FakeLLM(
        {
            "intake_grade": grade_handler(grades),
            "resolution_level": level_handler,
            "intake_fix": fix_handler(technician=technician),
            "verify_support": support_handler(support),
        }
    )
    instance = FakeServiceNow([*history, *open_tickets])
    reader = HumanNotesOnly(instance, ["SN Agent"])
    source = ParallelSearchSource(reader, cfg, llm, threads=3)
    return FixFinder(cfg, llm, source), llm, instance


VPN_ISSUE = IssueReport(summary="VPN client fails to connect with error 809", system="VPN", error_text="error 809")


# --- finding a fix ----------------------------------------------------------


def test_documented_fix_is_offered_with_its_source_ticket():
    finder, llm, _ = build(
        [make_ticket("INC0001", close_notes=VPN_FIX, assignment_group="Network Support")],
        grades={"INC0001": ("relevant", 0.9, "unrelated")},
    )

    result = finder.find(VPN_ISSUE)

    assert result.status == "fix_found"
    assert result.steps[0].tickets == ["INC0001"]
    assert result.group_votes == {"Network Support": 0.9}
    shown = result.for_model()
    assert shown["steps_for_user"][0]["from_tickets"] == ["INC0001"]
    # One pass: no planning call, no research loop, no agreement samples.
    assert "plan" not in llm.steps() and not any(s.startswith("generate") for s in llm.steps())


def test_the_agents_own_notes_are_never_evidence():
    """The self-citation hole: a ticket closed with no write-up, where the only
    'fix' is a suggestion the ticket agent itself posted, must not come back as
    a documented fix."""

    suggestion = f"{HEADER}\nSuggested resolution: {VPN_FIX} Restart the client and reconnect afterwards."
    past = make_ticket("INC0002", close_notes="Resolved", work_notes=[suggestion])
    past.journal.append(JournalEntry(element="work_notes", value=VPN_FIX + " (copied from the agent note)", created_by="SN Agent"))
    finder, _llm, _ = build([past], grades={"INC0002": ("relevant", 0.9, "unrelated")})

    result = finder.find(VPN_ISSUE)

    assert result.status == "no_documented_fix"
    assert result.steps == []
    assert "steps_for_user" not in result.for_model()


def test_unsupported_steps_are_dropped_not_offered():
    finder, _llm, _ = build(
        [make_ticket("INC0001", close_notes=VPN_FIX)],
        grades={"INC0001": ("relevant", 0.9, "unrelated")},
        support="unsupported",
    )

    result = finder.find(VPN_ISSUE)

    assert result.status == "no_documented_fix"
    assert result.steps == []


def test_steps_needing_a_technician_are_withheld_from_the_user():
    finder, _llm, _ = build(
        [make_ticket("INC0001", close_notes=VPN_FIX)],
        grades={"INC0001": ("relevant", 0.9, "unrelated")},
        technician=True,
    )

    shown = finder.find(VPN_ISSUE).for_model()

    assert shown["steps_for_user"] == []
    assert shown["steps_needing_technician"] == 1


def test_nothing_similar_means_no_history():
    finder, _llm, _ = build([], grades={})

    result = finder.find(IssueReport(summary="Badge reader at door 4 rejects my card", system="Badge reader"))

    assert result.status == "no_history"
    assert "Do not suggest a fix" in result.for_model()["note"]


# --- priority and team -------------------------------------------------------


def test_priority_follows_impact_and_urgency():
    assert decide_priority("just_me", "minor").priority == 5
    assert decide_priority("just_me", "cannot_work").priority == 3
    assert decide_priority("team", "degraded").priority == 3
    assert decide_priority("many", "cannot_work").priority == 1


def test_security_and_major_incidents_raise_priority():
    assert decide_priority("just_me", "minor", security=True).urgency == 1
    major = decide_priority("just_me", "degraded", major_incident=True, open_duplicates=3)
    assert major.impact == 1 and major.priority == 2
    assert any("major incident" in r for r in major.reasons)


class _Directory(GroupDirectory):
    def __init__(self, groups=None, history=None):
        super().__init__(client=None)
        self._fixed_groups = groups or {}
        self._fixed_history = history or {}

    def groups(self):
        return self._fixed_groups

    def category_history(self, category):
        return self._fixed_history.get(category, {})


def test_team_comes_from_who_fixed_it_before():
    chooser = GroupChooser(settings(), FakeLLM(), _Directory({"Network Support": "g1", "Service Desk": "g0"}))

    decision = chooser.choose(summary="VPN", system="VPN", category="network", votes={"Network Support": 1.6, "Desktop": 0.3})

    assert (decision.name, decision.sys_id, decision.method) == ("Network Support", "g1", "evidence")


def test_category_history_breaks_a_thin_vote():
    chooser = GroupChooser(
        settings(), FakeLLM(), _Directory({"Database Team": "g2", "Service Desk": "g0"}, {"database": {"Database Team": 12}})
    )

    decision = chooser.choose(summary="Reports slow", system="", category="database", votes={})

    assert decision.name == "Database Team" and decision.method == "category_history"


def test_no_signal_goes_to_the_default_team():
    chooser = GroupChooser(settings(), FakeLLM(), _Directory())

    decision = chooser.choose(summary="Something odd", system="", category="", votes={})

    assert decision.name == "Service Desk" and decision.method == "fallback"


# --- the session's tools ----------------------------------------------------


def make_session(finder, *, writer_client=None, cfg=None, store=None):
    cfg = cfg or settings()
    services = IntakeServices(
        settings=cfg,
        finder=finder,
        chooser=GroupChooser(cfg, FakeLLM(), _Directory({"Network Support": "g1", "Service Desk": "g0"})),
        writer=IncidentWriter(cfg, writer_client),
        store=store,
    )
    return IntakeSession(services, user_email="ana@example.com", channel="text")


def run(coro):
    return asyncio.run(coro)


def test_incident_is_only_drafted_while_creation_is_off():
    finder, _llm, _ = build(
        [make_ticket("INC0001", close_notes=VPN_FIX, assignment_group="Network Support")],
        grades={"INC0001": ("relevant", 0.9, "unrelated")},
    )
    store = Store(":memory:")
    session = make_session(finder, store=store)
    session.add_message("user", "VPN gives error 809 and I can't work")

    assert "error" in json.loads(run(session.call_tool("create_incident", {})))
    run(session.call_tool("search_past_fixes", {"summary": VPN_ISSUE.summary, "system": "VPN", "error_text": "809"}))
    prepared = json.loads(
        run(
            session.call_tool(
                "prepare_incident",
                {"short_description": "VPN error 809", "details": "Cannot connect", "users_affected": "just_me", "work_impact": "cannot_work"},
            )
        )
    )
    created = json.loads(run(session.call_tool("create_incident", {})))

    assert prepared["priority"] == 3 and prepared["team"] == "Network Support"
    assert created["status"] == "dry_run"
    payload = session.incident.payload
    assert payload["impact"] == "3" and payload["urgency"] == "1"
    assert payload["assignment_group"] == "g1"
    assert payload["correlation_id"] == "sn-intake-agent"
    assert "INC0001" in payload["description"]
    saved = store.get_conversation(session.id)
    assert saved["outcome"] == "ticket_drafted" and saved["assignment_group"] == "Network Support"


class _RecordingClient:
    base_url = "https://example.service-now.com"

    def __init__(self):
        self.posted = []
        self.notes = []

    def query_records(self, table, query, **kwargs):
        return iter([{"sys_id": {"value": "user-1"}}]) if table == "sys_user" else iter([])

    def _request(self, method, url, **kwargs):
        self.posted.append((method, url, kwargs["json"]))
        return SimpleNamespace(json=lambda: {"result": {"number": "INC0012345", "sys_id": "new-1"}})

    def post_work_note(self, table, sys_id, note):
        self.notes.append((sys_id, note))


def test_incident_is_created_through_the_intake_account_when_switched_on():
    finder, _llm, _ = build([], grades={})
    client = _RecordingClient()
    session = make_session(finder, writer_client=client, cfg=settings(intake_create_tickets=True))

    run(
        session.call_tool(
            "prepare_incident",
            {"short_description": "Printer jams", "details": "Floor 2 printer", "users_affected": "team", "work_impact": "degraded"},
        )
    )
    created = json.loads(run(session.call_tool("create_incident", {})))

    assert created == {"status": "created", "number": "INC0012345", "priority": 3, "team": "Service Desk"}
    method, url, body = client.posted[0]
    assert (method, url, body["caller_id"]) == ("POST", "/api/now/table/incident", "user-1")
    assert client.notes and "[Help desk assistant]" in client.notes[0][1]
    assert session.outcome == "ticket_raised"


def test_solved_conversations_count_as_deflected():
    finder, _llm, _ = build([], grades={})
    store = Store(":memory:")
    session = make_session(finder, store=store)
    session.add_message("user", "Outlook keeps asking for my password")
    run(session.call_tool("mark_solved", {"note": "signed out and back in"}))
    session.record_turn_latency(0.8, 2.0)

    board = store.dashboard()

    assert board["solved_without_ticket"] == 1 and board["deflection_rate"] == 1.0
    assert board["turn_first_token_s"]["p50"] == 0.8


# --- the streaming text channel ---------------------------------------------


def _chunk(content=None, tool_call=None):
    calls = None
    if tool_call:
        calls = [
            SimpleNamespace(
                index=0, id=tool_call[0], function=SimpleNamespace(name=tool_call[1], arguments=tool_call[2])
            )
        ]
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=calls))])


class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self.chunks:
                yield chunk

        return gen()


class _ScriptedClient:
    """Answers each completion call with the next scripted stream."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return _Stream(self.scripts.pop(0))


def test_text_chat_streams_a_filler_while_searching_then_the_answer():
    finder, _llm, _ = build(
        [make_ticket("INC0001", close_notes=VPN_FIX)],
        grades={"INC0001": ("relevant", 0.9, "unrelated")},
    )
    session = make_session(finder)
    args = json.dumps({"summary": VPN_ISSUE.summary, "system": "VPN", "error_text": "809"})
    client = _ScriptedClient(
        [
            [_chunk(tool_call=("call-1", "search_past_fixes", args))],
            [_chunk("This was fixed before on INC0001. "), _chunk("First, reconnect.")],
        ]
    )
    chat = TextChat(session, client, "fake-model")

    async def collect():
        return [part async for part in chat.reply("VPN error 809 since this morning")]

    parts = run(collect())

    assert parts[0] == SEARCH_FILLER
    assert "".join(parts).endswith("First, reconnect.")
    tool_message = next(m for m in chat.messages if m["role"] == "tool")
    assert json.loads(tool_message["content"])["status"] == "fix_found"
    assert session.events[-1]["type"] == "turn"
