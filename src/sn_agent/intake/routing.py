"""Priority and assignment group for a new incident.

The model only gathers the facts; these rules make the decision, so the same
facts always give the same answer and the reason can be written on the ticket.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from ..config import Settings
from ..llm import LLMClient, Meter, Tier
from .prompts import GROUP_PROMPT, GROUP_SYSTEM

log = logging.getLogger(__name__)

UsersAffected = Literal["just_me", "team", "many"]
WorkImpact = Literal["cannot_work", "degraded", "minor"]

IMPACT = {"many": 1, "team": 2, "just_me": 3}
URGENCY = {"cannot_work": 1, "degraded": 2, "minor": 3}

# ServiceNow's default priority lookup (impact, urgency) -> priority. The
# instance computes priority itself from these two fields and overwrites any
# priority sent directly, so this table is only used to read it back.
PRIORITY_MATRIX = {
    (1, 1): 1, (1, 2): 2, (1, 3): 3,
    (2, 1): 2, (2, 2): 3, (2, 3): 4,
    (3, 1): 3, (3, 2): 4, (3, 3): 5,
}
PRIORITY_LABEL = {1: "Critical", 2: "High", 3: "Moderate", 4: "Low", 5: "Planning"}

# A group needs at least this share of the evidence vote, and this much weight,
# to be chosen on evidence alone.
MIN_VOTE_SHARE = 0.5
MIN_VOTE_WEIGHT = 0.4
GROUP_CACHE_S = 3600
CATEGORY_HISTORY_DAYS = 90


@dataclass
class PriorityDecision:
    impact: int
    urgency: int
    priority: int
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"P{self.priority} - {PRIORITY_LABEL[self.priority]}"


def decide_priority(
    users_affected: str, work_impact: str, *, security: bool = False, major_incident: bool = False,
    open_duplicates: int = 0,
) -> PriorityDecision:
    impact = IMPACT.get(users_affected, 3)
    urgency = URGENCY.get(work_impact, 2)
    reasons = [
        {"many": "many people affected", "team": "a team affected", "just_me": "one person affected"}.get(
            users_affected, "one person affected"
        ),
        {"cannot_work": "cannot work", "degraded": "can work, but degraded", "minor": "minor inconvenience"}.get(
            work_impact, "can work, but degraded"
        ),
    ]
    if major_incident and impact > 1:
        impact = 1
        reasons.append(f"possible major incident: {open_duplicates} other open ticket(s) for the same issue")
    elif open_duplicates and impact > 2:
        impact = 2
        reasons.append(f"{open_duplicates} other open ticket(s) for the same issue")
    if security and urgency > 1:
        urgency = 1
        reasons.append("security issue")
    return PriorityDecision(impact=impact, urgency=urgency, priority=PRIORITY_MATRIX[(impact, urgency)], reasons=reasons)


@dataclass
class GroupDecision:
    name: str
    sys_id: str = ""
    method: str = "fallback"  # evidence | category_history | model | fallback
    reason: str = ""
    votes: dict[str, float] = field(default_factory=dict)


class _GroupChoice(BaseModel):
    group: str = ""
    reason: str = ""


class GroupDirectory:
    """Active assignment groups and, per category, which groups have resolved
    tickets lately. Both are read from ServiceNow and cached for an hour.

    Reading `sys_user_group` may be refused to a least-privilege account; then
    the directory is empty, and groups are named from past tickets alone."""

    def __init__(self, client: Any):
        self.client = client
        self._groups: dict[str, str] = {}
        self._groups_at = 0.0
        self._history: dict[str, tuple[float, dict[str, int]]] = {}
        self._lock = threading.Lock()

    def groups(self) -> dict[str, str]:
        """name -> sys_id"""

        with self._lock:
            if self._groups and time.time() - self._groups_at < GROUP_CACHE_S:
                return self._groups
            try:
                rows = self.client.query_records(
                    "sys_user_group", "active=true^ORDERBYname", fields=["sys_id", "name"], limit=500
                )
                self._groups = {_plain(r, "name"): _plain(r, "sys_id") for r in rows if _plain(r, "name")}
            except Exception as exc:  # noqa: BLE001 - a missing directory must not stop the chat
                log.warning("could not read assignment groups (%s); naming groups from past tickets only", exc)
                self._groups = {}
            self._groups_at = time.time()
            return self._groups

    def category_history(self, category: str) -> dict[str, int]:
        if not category:
            return {}
        with self._lock:
            cached = self._history.get(category)
            if cached and time.time() - cached[0] < GROUP_CACHE_S:
                return cached[1]
        counts: dict[str, int] = {}
        try:
            rows = self.client.query_records(
                "incident",
                f"category={category}^resolved_atISNOTEMPTY"
                f"^opened_at>=javascript:gs.daysAgoStart({CATEGORY_HISTORY_DAYS})",
                fields=["assignment_group"],
                limit=300,
            )
            for row in rows:
                name = _display(row, "assignment_group")
                if name:
                    counts[name] = counts.get(name, 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read category history for %s: %s", category, exc)
        with self._lock:
            self._history[category] = (time.time(), counts)
        return counts

    def warm(self) -> None:
        self.groups()


class GroupChooser:
    def __init__(self, settings: Settings, llm: LLMClient, directory: GroupDirectory):
        self.settings = settings
        self.llm = llm
        self.directory = directory

    def choose(self, *, summary: str, system: str, category: str, votes: dict[str, float]) -> GroupDecision:
        known = self.directory.groups()
        fallback = self.settings.intake_default_group

        def decision(name: str, method: str, reason: str) -> GroupDecision:
            canonical = _match(name, known) or name
            return GroupDecision(
                name=canonical, sys_id=known.get(canonical, ""), method=method, reason=reason, votes=votes
            )

        total = sum(votes.values())
        if votes:
            top, weight = max(votes.items(), key=lambda kv: kv[1])
            if weight >= MIN_VOTE_WEIGHT and weight / total >= MIN_VOTE_SHARE and (not known or _match(top, known)):
                return decision(top, "evidence", f"resolved {_share(weight, total)} of the most similar past tickets")

        history = self.directory.category_history(category)
        if history:
            merged = dict(votes)
            # Category history is weaker evidence than a matching ticket, so it
            # only tips the balance.
            scale = 0.5 / max(history.values())
            for name, count in history.items():
                merged[name] = merged.get(name, 0.0) + count * scale
            top, weight = max(merged.items(), key=lambda kv: kv[1])
            if weight / sum(merged.values()) >= MIN_VOTE_SHARE and (not known or _match(top, known)):
                return decision(top, "category_history", f"most often resolves {category} incidents lately")

        names = list(known) or sorted(votes)
        if names:
            try:
                choice = self.llm.generate_json(
                    tier=Tier.LITE,
                    system=GROUP_SYSTEM,
                    prompt=GROUP_PROMPT.format(
                        summary=summary,
                        system=system or "-",
                        category=category or "-",
                        votes=", ".join(f"{k} ({v:.2f})" for k, v in sorted(votes.items(), key=lambda kv: -kv[1]))
                        or "none",
                        groups="\n".join(f"- {n}" for n in names[:150]),
                        fallback=fallback,
                    ),
                    schema=_GroupChoice,
                    meter=Meter(self.settings.prices, 1.0, 50_000),
                    step="intake_group",
                )
                picked = _match(choice.group, known) or (choice.group if choice.group in votes else "")
                if picked:
                    return decision(picked, "model", choice.reason or "best fit for the issue")
            except Exception as exc:  # noqa: BLE001 - fall through to the default team
                log.warning("group choice by model failed: %s", exc)

        return decision(fallback, "fallback", "no team stood out, so it goes to the default team")


def _share(weight: float, total: float) -> str:
    return f"{round(100 * weight / total)}%" if total else "most"


def _match(name: str, known: dict[str, str]) -> str:
    if not name:
        return ""
    if name in known:
        return name
    lowered = name.strip().lower()
    return next((k for k in known if k.lower() == lowered), "")


def _plain(record: dict[str, Any], field_name: str) -> str:
    raw = record.get(field_name)
    if isinstance(raw, dict):
        return str(raw.get("value") or raw.get("display_value") or "")
    return str(raw or "")


def _display(record: dict[str, Any], field_name: str) -> str:
    raw = record.get(field_name)
    if isinstance(raw, dict):
        return str(raw.get("display_value") or raw.get("value") or "")
    return str(raw or "")
