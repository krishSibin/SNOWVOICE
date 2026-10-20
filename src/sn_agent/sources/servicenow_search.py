"""Evidence, searched live in ServiceNow.

No copy of ticket data is kept anywhere: every run asks the instance directly and
holds the results only for the length of the run. Two complementary passes make
up for the loss of a semantic index:

1. Full-text search (Zing) for each query the planner wrote. Strong on error
   codes and shared vocabulary, blind when the wording differs entirely.
2. Scoped sweeps by configuration item and category. These catch the same issue
   described in completely different words, which full-text search misses.

Results are merged by rank, so a ticket found by several routes rises.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from ..config import Settings
from ..ingest.chunking import chunk_ticket
from ..ingest.resolution_level import classify_resolution_level
from ..llm import LLMClient, Meter
from ..models import Candidate, ResolutionLevel, SearchScope, Ticket
from ..servicenow.client import ServiceNowClient

log = logging.getLogger(__name__)

RRF_K = 60
HISTORY_TABLES = ("incident", "problem")


class ServiceNowSearchSource:
    name = "servicenow_live"

    def __init__(self, client: ServiceNowClient, settings: Settings, llm: LLMClient):
        self.client = client
        self.settings = settings
        self.llm = llm

    # --- closed history -----------------------------------------------------

    def search(
        self, *, ticket: Ticket, queries: list[str], scope: SearchScope, limit: int, meter: Meter
    ) -> list[Candidate]:
        ranked: list[list[tuple[str, dict[str, Any], str]]] = []

        for table in HISTORY_TABLES:
            clauses = self._history_clauses(table, ticket, scope)
            for query in queries:
                rows = self.client.search_text(
                    table, query, clauses=clauses, limit=self.settings.search_results_per_query
                )
                log.debug('   %s full-text "%s": %d hit(s)', table, query, len(rows))
                ranked.append([(table, row, "text") for row in rows])
            for rows in self._sweeps(table, ticket, scope, clauses):
                log.debug("   %s sweep by system/category: %d hit(s)", table, len(rows))
                ranked.append([(table, row, "sweep") for row in rows])

        return self._to_candidates(ranked, limit, meter)

    def _sweeps(
        self, table: str, ticket: Ticket, scope: SearchScope, clauses: list[str]
    ) -> list[list[dict[str, Any]]]:
        """Recent tickets on the same configuration item or in the same category,
        regardless of wording."""

        if scope.level >= 2:
            return []
        scopes: list[dict[str, str]] = []
        if scope.level == 0 and ticket.cmdb_ci_id:
            scopes.append({"cmdb_ci": ticket.cmdb_ci_id})
        if scope.level == 0 and ticket.business_service:
            scopes.append({"business_service": ticket.business_service})
        if ticket.category:
            scopes.append({"category": ticket.category})
        return [
            self.client.search_by_fields(
                table,
                equals=fields,
                clauses=clauses,
                limit=self.settings.search_sweep_limit,
                order_by="ORDERBYDESCopened_at",
            )
            for fields in scopes
        ]

    def _history_clauses(self, table: str, ticket: Ticket, scope: SearchScope) -> list[str]:
        clauses = [f"number!={ticket.number}"]
        if table == "incident":
            clauses.append("resolved_atISNOTEMPTY")
        else:
            clauses.append("fix_notesISNOTEMPTY^ORcause_notesISNOTEMPTY^ORworkaroundISNOTEMPTY")
        if scope.recency_days:
            clauses.append(f"opened_at>=javascript:gs.daysAgoStart({scope.recency_days})")
        if scope.as_of:
            # Evaluation only: nothing written after the ticket was raised.
            stamp = scope.as_of.strftime("%Y-%m-%d")
            clauses.append(f"opened_at<javascript:gs.dateGenerate('{stamp}','00:00:00')")
        return clauses

    # --- open tickets -------------------------------------------------------

    def find_open(self, ticket: Ticket, queries: list[str], meter: Meter) -> list[Candidate]:
        """Open tickets describing the same thing right now. Always live, because
        their state and work notes change by the hour."""

        clauses = [
            "active=true",
            f"number!={ticket.number}",
            f"opened_at>=javascript:gs.daysAgoStart({self.settings.open_ticket_window_days})",
        ]
        ranked: list[list[tuple[str, dict[str, Any], str]]] = []
        for query in queries[: self.settings.open_ticket_queries]:
            rows = self.client.search_text(
                "incident", query, clauses=clauses, limit=self.settings.search_results_per_query
            )
            ranked.append([("incident", row, "text") for row in rows])
        for fields in self._open_scopes(ticket):
            rows = self.client.search_by_fields(
                "incident", equals=fields, clauses=clauses, limit=self.settings.search_sweep_limit
            )
            ranked.append([("incident", row, "sweep") for row in rows])
        return self._to_candidates(ranked, self.settings.open_ticket_max_candidates, meter)

    def _open_scopes(self, ticket: Ticket) -> list[dict[str, str]]:
        scopes = []
        if ticket.cmdb_ci_id:
            scopes.append({"cmdb_ci": ticket.cmdb_ci_id})
        if ticket.business_service:
            scopes.append({"business_service": ticket.business_service})
        return scopes

    # --- shared -------------------------------------------------------------

    def _to_candidates(
        self, ranked: list[list[tuple[str, dict[str, Any], str]]], limit: int, meter: Meter
    ) -> list[Candidate]:
        scores: dict[tuple[str, str], float] = defaultdict(float)
        records: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for results in ranked:
            for rank, (table, row, route) in enumerate(results):
                ticket = self.client.to_ticket(row, table)
                if not ticket.number:
                    continue
                key = (table, ticket.number)
                # A sweep hit is weaker evidence of relevance than a text hit:
                # it only means "same component", not "similar wording".
                weight = 1.0 if route == "text" else 0.6
                scores[key] += weight / (RRF_K + rank + 1)
                records.setdefault(key, (table, row))

        top = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:limit]
        if not top:
            return []

        tickets: list[tuple[Ticket, float]] = []
        by_table: dict[str, list[str]] = defaultdict(list)
        for (table, _number), score in top:
            key = (table, _number)
            ticket = self.client.to_ticket(records[key][1], table)
            tickets.append((ticket, score))
            by_table[table].append(ticket.sys_id)

        journals: dict[str, list] = {}
        for table, sys_ids in by_table.items():
            journals.update(self.client.get_journal(table, sys_ids))

        candidates: list[Candidate] = []
        for ticket, score in tickets:
            ticket.journal = journals.get(ticket.sys_id, [])
            level = (
                ResolutionLevel.NONE
                if ticket.active
                else classify_resolution_level(ticket, llm=self.llm, meter=meter)
            )
            chunks = chunk_ticket(ticket, level, max_chars=self.settings.chunk_max_chars)
            if chunks:
                candidates.append(Candidate.from_chunks(chunks, score))
        return candidates
