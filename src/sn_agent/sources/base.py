from __future__ import annotations

from typing import Protocol

from ..llm import Meter
from ..models import Candidate, SearchScope, Ticket


class KnowledgeSource(Protocol):
    """Where evidence comes from. v1 searches ServiceNow tickets live; knowledge
    articles, runbooks and web search plug in here later without the pipeline
    changing."""

    name: str

    def search(
        self, *, ticket: Ticket, queries: list[str], scope: SearchScope, limit: int, meter: Meter
    ) -> list[Candidate]: ...

    def find_open(self, ticket: Ticket, queries: list[str], meter: Meter) -> list[Candidate]: ...
