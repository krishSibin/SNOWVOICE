"""Build the offline test set, straight from ServiceNow.

Every case is a past ticket whose own resolution is hidden from the agent, and
retrieval is cut off at the date it was raised, so nothing written afterwards can
leak into the answer. Only ticket numbers and metrics are written to disk — the
ticket text stays in ServiceNow.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from sn_agent.models import Ticket
from sn_agent.servicenow.client import ServiceNowClient


class EvalCase(BaseModel):
    number: str
    table: str = "incident"

    def load(self, client: ServiceNowClient) -> tuple[Ticket, str]:
        """Fetches the ticket at evaluation time and splits off its real
        resolution, which is the answer key."""

        ticket = client.get_ticket(self.number, self.table)
        actual = ticket.resolution_text
        asked = ticket.model_copy(
            update={
                "close_notes": "",
                "extra_resolution": {},
                "journal": [],
                "resolved_at": None,
                "active": True,
            }
        )
        return asked, actual


def build_cases(
    client: ServiceNowClient, *, opened_after: datetime, limit: int = 100, table: str = "incident"
) -> list[EvalCase]:
    """Only tickets that recorded a real resolution can be scored against."""

    stamp = opened_after.strftime("%Y-%m-%d")
    query = (
        "resolved_atISNOTEMPTY^close_notesISNOTEMPTY"
        f"^opened_at>=javascript:gs.dateGenerate('{stamp}','00:00:00')"
        "^ORDERBYDESCopened_at"
    )
    cases: list[EvalCase] = []
    for record in client.query_records(table, query, limit=limit * 3):
        ticket = client.to_ticket(record, table)
        if len(ticket.resolution_text.strip()) < 60:
            continue  # nothing substantial to score an answer against
        cases.append(EvalCase(number=ticket.number, table=table))
        if len(cases) >= limit:
            break
    return cases


def save(cases: list[EvalCase], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([c.model_dump(mode="json") for c in cases], indent=2), encoding="utf-8"
    )


def load(path: Path) -> list[EvalCase]:
    return [EvalCase.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8"))]
