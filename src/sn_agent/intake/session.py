"""One conversation with one employee, and the tools the conversation model
can call. Shared by the text chat and the LiveKit voice agent, so both channels
behave identically: same search, same rules, same ticket.

The model decides *when* to search or raise a ticket. What a search may offer,
which priority and which team a ticket gets, and whether a ticket is created at
all are decided here, in code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..config import Settings
from ..ingest.redact import redact
from ..llm import TcsGenAiLLM
from ..servicenow.client import ServiceNowClient
from ..store import Store
from .evidence import FixFinder, FixSearch, HumanNotesOnly, IssueReport, ParallelSearchSource
from .routing import GroupChooser, GroupDecision, GroupDirectory, PriorityDecision, decide_priority
from .tickets import CreatedIncident, IncidentDraft, IncidentWriter, intake_client

log = logging.getLogger(__name__)

CATEGORIES = ["network", "software", "hardware", "database", "inquiry"]
TRANSCRIPT_TURNS_IN_NOTE = 30


@dataclass
class IntakeServices:
    """Everything a session needs, built once per process."""

    settings: Settings
    finder: FixFinder
    chooser: GroupChooser
    writer: IncidentWriter
    store: Store | None = None

    def warm(self) -> None:
        """Loads the group list now, so the first ticket does not wait for it."""

        try:
            self.chooser.directory.warm()
        except Exception as exc:  # noqa: BLE001
            log.warning("warm-up skipped: %s", exc)


def build_services(settings: Settings, *, store: Store | None = None) -> IntakeServices:
    llm = TcsGenAiLLM(settings)
    reader = HumanNotesOnly(
        ServiceNowClient(settings),
        [a for a in settings.intake_agent_note_authors.split(",") if a.strip()],
    )
    source = ParallelSearchSource(reader, settings, llm, threads=settings.intake_search_threads)
    writer_client = intake_client(settings)
    # Group and user lookups need more read rights than the ticket agent's
    # account has; the intake account is the one expected to have them.
    directory = GroupDirectory(writer_client or reader)
    return IntakeServices(
        settings=settings,
        finder=FixFinder(settings, llm, source),
        chooser=GroupChooser(settings, llm, directory),
        writer=IncidentWriter(settings, writer_client),
        store=store,
    )


# --- tool definitions, in the OpenAI function-calling format ---------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_past_fixes",
            "description": (
                "Search resolved past tickets for a documented fix to the user's issue. Call as soon as "
                "you know the symptom and the system. Returns only steps that past tickets document."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One-line summary in an engineer's words"},
                    "system": {"type": "string", "description": "Application, device or service affected"},
                    "error_text": {"type": "string", "description": "Exact error message or code, if any"},
                    "details": {"type": "string", "description": "Other relevant details the user gave"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["summary", "system"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "prepare_incident",
            "description": (
                "Work out the priority and support team for a new incident and return them for you to read "
                "back to the user. Creates nothing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "short_description": {"type": "string", "description": "One line, under 100 characters"},
                    "details": {"type": "string", "description": "What the user reported, in full"},
                    "users_affected": {"type": "string", "enum": ["just_me", "team", "many"]},
                    "work_impact": {"type": "string", "enum": ["cannot_work", "degraded", "minor"]},
                    "security_related": {"type": "boolean", "description": "Phishing, malware, account compromise"},
                    "steps_tried": {"type": "string", "description": "What the user already tried, including steps from this chat"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["short_description", "details", "users_affected", "work_impact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_incident",
            "description": "Raise the incident prepared by prepare_incident. Only after the user said yes to the read-back.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mark_solved",
            "description": "Record that the issue is fixed and no ticket is needed. Call when the user confirms it works.",
            "parameters": {
                "type": "object",
                "properties": {"note": {"type": "string", "description": "What fixed it"}},
            },
        },
    },
]


@dataclass
class _Prepared:
    draft: IncidentDraft
    priority: PriorityDecision
    group: GroupDecision


class IntakeSession:
    def __init__(
        self,
        services: IntakeServices,
        *,
        user_email: str = "",
        user_name: str = "",
        channel: str = "text",
        session_id: str | None = None,
    ):
        self.services = services
        self.id = session_id or uuid.uuid4().hex[:16]
        self.user_email = user_email.strip().lower()
        self.user_name = user_name
        self.channel = channel
        self.started_at = time.time()
        self.transcript: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.outcome = "open"
        self.summary = ""
        self.last_search: FixSearch | None = None
        self.last_issue: IssueReport | None = None
        self.prepared: _Prepared | None = None
        self.incident: CreatedIncident | None = None
        self._lock = asyncio.Lock()

    # --- transcript ---------------------------------------------------------

    def add_message(self, role: str, text: str, *, channel: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        if channel and channel != self.channel and role == "user":
            # A session that started in text and moved to voice (or back) is
            # recorded under the channel the user ended up using most.
            self.channel = "voice" if "voice" in (channel, self.channel) else channel
        self.transcript.append({"role": role, "text": text, "at": time.time()})
        self.save()

    def add_event(self, kind: str, **data: Any) -> None:
        self.events.append({"type": kind, "at": time.time(), **data})

    def record_turn_latency(self, first_token_s: float, total_s: float | None = None) -> None:
        self.add_event("turn", first_token_s=round(first_token_s, 3), total_s=round(total_s or 0.0, 3))
        self.save()

    # --- tools --------------------------------------------------------------

    async def call_tool(self, name: str, arguments: dict[str, Any] | str) -> str:
        """Runs one tool and returns its result as JSON text for the model."""

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
        handler = {
            "search_past_fixes": self.search_past_fixes,
            "prepare_incident": self.prepare_incident,
            "create_incident": self.create_incident,
            "mark_solved": self.mark_solved,
        }.get(name)
        if handler is None:
            return json.dumps({"error": f"unknown tool {name}"})
        started = time.time()
        async with self._lock:
            try:
                result = await handler(**{k: v for k, v in arguments.items() if v is not None})
            except TypeError as exc:
                result = {"error": f"bad arguments: {exc}"}
            except Exception as exc:  # noqa: BLE001 - the conversation must carry on
                log.exception("tool %s failed", name)
                result = {"error": "the tool failed; apologise and offer to raise a ticket", "detail": str(exc)[:200]}
        log.info("tool %s took %.2fs", name, time.time() - started)
        self.add_event("tool", name=name, arguments=arguments, seconds=round(time.time() - started, 2), result=result)
        self.save()
        return json.dumps(result, default=str)

    async def search_past_fixes(
        self, summary: str, system: str = "", error_text: str = "", details: str = "", category: str = ""
    ) -> dict[str, Any]:
        issue = IssueReport(
            summary=summary, system=system, error_text=error_text, details=details,
            category=category if category in CATEGORIES else "",
        )
        self.last_issue = issue
        self.summary = summary
        search = await asyncio.to_thread(self.services.finder.find, issue)
        self.last_search = search
        return search.for_model()

    async def prepare_incident(
        self,
        short_description: str,
        details: str,
        users_affected: str = "just_me",
        work_impact: str = "degraded",
        security_related: bool = False,
        steps_tried: str = "",
        category: str = "",
    ) -> dict[str, Any]:
        search = self.last_search
        issue = self.last_issue or IssueReport(summary=short_description, details=details)
        if search is None:
            # Raising a ticket without having searched still benefits from the
            # search: it finds the team that fixes this and any open duplicates.
            issue = IssueReport(summary=short_description, details=details, category=category)
            search = await asyncio.to_thread(self.services.finder.find, issue)
            self.last_search, self.last_issue = search, issue

        priority = decide_priority(
            users_affected, work_impact, security=bool(security_related),
            major_incident=search.major_incident, open_duplicates=len(search.open_now),
        )
        category = category if category in CATEGORIES else (
            issue.category or (max(search.category_votes, key=search.category_votes.get) if search.category_votes else "")
        )
        group = await asyncio.to_thread(
            self.services.chooser.choose,
            summary=short_description, system=issue.system, category=category, votes=search.group_votes,
        )
        draft = IncidentDraft(
            short_description=short_description,
            description=self._description(details, steps_tried, search, priority, group),
            impact=priority.impact,
            urgency=priority.urgency,
            expected_priority=priority.priority,
            assignment_group=group.name,
            assignment_group_sys_id=group.sys_id,
            category=category,
            caller_email=self.user_email,
            work_note=self._work_note(priority, group, search),
            channel=self.channel,
        )
        self.prepared = _Prepared(draft=draft, priority=priority, group=group)
        self.summary = short_description
        return {
            "read_back": f"{short_description}. Priority {priority.priority} ({priority.label.split(' - ')[1]}), "
            f"for the {group.name} team.",
            "priority": priority.priority,
            "team": group.name,
            "why_priority": "; ".join(priority.reasons),
            "why_team": group.reason,
            "next": "Read this back and ask the user to confirm before calling create_incident.",
        }

    async def create_incident(self) -> dict[str, Any]:
        if self.incident is not None and self.incident.created:
            return {"status": "already_created", "number": self.incident.number}
        if self.prepared is None:
            return {"error": "Call prepare_incident first and read it back to the user."}
        created = await asyncio.to_thread(self.services.writer.create, self.prepared.draft)
        self.incident = created
        self.outcome = "ticket_raised" if created.created else "ticket_drafted"
        if created.created:
            return {
                "status": "created",
                "number": created.number,
                "priority": self.prepared.priority.priority,
                "team": self.prepared.group.name,
            }
        return {
            "status": "dry_run",
            "note": (
                "Ticket creation is switched off on this system, so no ticket was raised. Tell the user "
                "their request was recorded and the IT team can see it, but there is no ticket number yet."
            ),
        }

    async def mark_solved(self, note: str = "") -> dict[str, Any]:
        if self.incident is None:
            self.outcome = "solved"
        self.add_event("solved", note=note)
        return {"status": "recorded"}

    # --- ticket text --------------------------------------------------------

    def _description(
        self, details: str, steps_tried: str, search: FixSearch, priority: PriorityDecision, group: GroupDecision
    ) -> str:
        lines = [redact(details).strip(), ""]
        if steps_tried.strip():
            lines += ["Already tried:", redact(steps_tried).strip(), ""]
        if search.steps:
            lines.append("Steps suggested by the help desk assistant from past tickets:")
            lines += [f"- {s.text} (from {', '.join(s.tickets)})" for s in search.steps]
            lines.append("")
        if search.open_now:
            lines += [f"Possibly the same issue as open ticket(s): {', '.join(search.open_now)}", ""]
        if search.similar_without_fix:
            lines += [f"Similar past ticket(s) with no recorded fix: {', '.join(search.similar_without_fix[:5])}", ""]
        lines.append(f"Raised by the help desk assistant ({self.channel} conversation {self.id}).")
        return "\n".join(lines).strip()

    def _work_note(self, priority: PriorityDecision, group: GroupDecision, search: FixSearch) -> str:
        votes = ", ".join(f"{k} {v:.2f}" for k, v in sorted(group.votes.items(), key=lambda kv: -kv[1])) or "none"
        lines = [
            "[Help desk assistant]",
            f"Priority: impact {priority.impact}, urgency {priority.urgency} -> {priority.label} "
            f"({'; '.join(priority.reasons)}).",
            f"Team: {group.name}, chosen by {group.method.replace('_', ' ')}: {group.reason}. Evidence votes: {votes}.",
            f"Search queries: {' | '.join(search.queries) or '-'}",
            "",
            "Conversation:",
        ]
        for turn in self.transcript[-TRANSCRIPT_TURNS_IN_NOTE:]:
            who = "User" if turn["role"] == "user" else "Assistant"
            lines.append(f"{who}: {redact(turn['text'])}")
        return "\n".join(lines)

    def close(self) -> None:
        """The user left. A conversation that ended without a fix or a ticket
        counts as abandoned on the dashboard."""

        if self.outcome == "open" and any(t["role"] == "user" for t in self.transcript):
            self.outcome = "abandoned"
        self.save()

    # --- persistence --------------------------------------------------------

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_email": self.user_email,
            "channel": self.channel,
            "started_at": self.started_at,
            "outcome": self.outcome,
            "summary": self.summary,
            "incident_number": self.incident.number if self.incident else "",
            "priority": self.prepared.priority.priority if self.prepared else None,
            "assignment_group": self.prepared.group.name if self.prepared else "",
            "transcript": self.transcript,
            "events": self.events,
            "search": asdict(self.last_search) if self.last_search else None,
        }

    def save(self) -> None:
        store = self.services.store
        if store is None:
            return
        try:
            record = self.record()
            record["events"] = [*record["events"], {"type": "search_detail", "search": record.pop("search")}]
            store.save_conversation(record)
        except Exception as exc:  # noqa: BLE001 - losing a log line must not end the chat
            log.warning("could not save conversation %s: %s", self.id, exc)
