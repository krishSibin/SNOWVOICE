"""ServiceNow Table API client.

Read access to incident/problem/change plus journal entries, and exactly one
write: appending a work note. Nothing here can change state, assignment or close
fields, and the integration user should not be granted those rights either.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import httpx

from ..config import Settings
from ..models import JournalEntry, Ticket, parse_sn_datetime
from ..tls import ssl_context

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}

ERROR_HINTS = {
    401: "login rejected, check SERVICENOW_USERNAME and SERVICENOW_PASSWORD in .env",
    403: "this account is not allowed to do that",
    404: "not found; check the ticket number and SERVICENOW_INSTANCE_URL",
}

# ServiceNow's full-text search operator, usable inside an encoded query.
ZING_OPERATOR = "123TEXTQUERY321"

INCIDENT_FIELDS = [
    "sys_id", "number", "short_description", "description", "state", "active", "priority",
    "category", "subcategory", "cmdb_ci", "business_service", "assignment_group", "assigned_to",
    "resolved_by", "opened_at", "resolved_at", "closed_at", "sys_updated_on", "close_code",
    "close_notes", "reopen_count", "problem_id", "rfc", "caused_by",
]
PROBLEM_FIELDS = [
    "sys_id", "number", "short_description", "description", "state", "active", "priority",
    "category", "cmdb_ci", "business_service", "assignment_group", "assigned_to", "resolved_by",
    "opened_at", "resolved_at", "sys_updated_on", "close_notes", "cause_notes", "fix_notes",
    "workaround", "rfc",
]
CHANGE_FIELDS = [
    "sys_id", "number", "short_description", "description", "state", "active", "category",
    "cmdb_ci", "business_service", "assignment_group", "assigned_to", "opened_at", "closed_at",
    "sys_updated_on", "close_notes", "close_code",
]

TABLE_FIELDS = {
    "incident": INCIDENT_FIELDS,
    "problem": PROBLEM_FIELDS,
    "change_request": CHANGE_FIELDS,
}

# Fields that carry a resolution on each table, beyond close_notes.
EXTRA_RESOLUTION_FIELDS = {
    "problem": ["cause_notes", "fix_notes", "workaround"],
    "incident": [],
    "change_request": [],
}

# Problems and changes are knowledge, not live duplicates: a problem record with a
# documented workaround is useful whether or not it was ever formally closed, and
# it should never be reported as "someone else has this open right now".
KNOWLEDGE_TABLES = {"problem", "change_request"}

# Resolved / Closed / Canceled. Many instances leave `active` true on a resolved
# incident until it auto-closes, so state and resolved_at decide, not that flag.
CLOSED_INCIDENT_STATES = {"6", "7", "8"}


class ServiceNowError(RuntimeError):
    pass


JOURNAL_HEADER = re.compile(
    r"^(?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - (?P<who>.*?) \((?P<kind>Work notes|Additional comments)\)[ \t]*$",
    re.MULTILINE,
)
JOURNAL_KIND = {"Work notes": "work_notes", "Additional comments": "comments"}


def parse_journal(text: str, element: str) -> list[JournalEntry]:
    """Splits a journal field's display value into entries. Each entry starts
    with a header line: `2026-09-17 11:34:56 - Jane Doe (Work notes)`.

    Timestamps are in the reading account's timezone, which the agent's account
    has set to UTC."""

    if not text:
        return []
    headers = list(JOURNAL_HEADER.finditer(text))
    entries: list[JournalEntry] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        entries.append(
            JournalEntry(
                element=JOURNAL_KIND.get(header.group("kind"), element),
                value=text[header.end() : end].strip(),
                created_on=parse_sn_datetime(header.group("when")),
                created_by=header.group("who"),
            )
        )
    return entries


def _describe(url: str, params: dict[str, Any] | None) -> str:
    """`/api/now/table/incident` plus its query, short enough to read in a log."""

    table = url.rsplit("/api/now/table/", 1)[-1]
    query = (params or {}).get("sysparm_query", "")
    if len(query) > 90:
        query = query[:87] + "..."
    return f'{table} "{query}"' if query else table


def _clean_search_text(text: str) -> str:
    """`^` and `=` are the encoded-query separators, so search text containing
    them would silently split the query into other conditions."""

    cleaned = re.sub(r"[\^=]", " ", text or "")
    return " ".join(cleaned.split())[:200]


def _value(record: dict[str, Any], field: str) -> str:
    """Records are fetched with sysparm_display_value=all, so each field is
    {"value": ..., "display_value": ...}."""

    raw = record.get(field)
    if isinstance(raw, dict):
        return str(raw.get("value") or "")
    return str(raw or "")


def _display(record: dict[str, Any], field: str) -> str:
    raw = record.get(field)
    if isinstance(raw, dict):
        return str(raw.get("display_value") or raw.get("value") or "")
    return str(raw or "")


class ServiceNowClient:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.settings = settings
        self.base_url = settings.servicenow_instance_url.rstrip("/")
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._last_request_at = 0.0
        auth = None
        if settings.servicenow_auth_mode == "basic":
            auth = (settings.servicenow_username, settings.servicenow_password.get_secret_value())
        self._client = client or httpx.Client(
            base_url=self.base_url or "https://invalid.invalid",
            timeout=settings.servicenow_timeout_s,
            auth=auth,
            headers={"Accept": "application/json"},
            verify=ssl_context(),
        )

    def close(self) -> None:
        self._client.close()

    # --- auth ---------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        if self.settings.servicenow_auth_mode != "oauth":
            return {}
        if self._token and time.time() < self._token_expires_at - 60:
            return {"Authorization": f"Bearer {self._token}"}
        data = {
            "client_id": self.settings.servicenow_client_id,
            "client_secret": self.settings.servicenow_client_secret.get_secret_value(),
        }
        if self.settings.servicenow_username:
            data.update(
                grant_type="password",
                username=self.settings.servicenow_username,
                password=self.settings.servicenow_password.get_secret_value(),
            )
        else:
            data["grant_type"] = "client_credentials"
        response = self._client.post("/oauth_token.do", data=data)
        if response.status_code >= 400:
            raise ServiceNowError(f"OAuth token request failed: {response.status_code} {response.text[:200]}")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 1800))
        return {"Authorization": f"Bearer {self._token}"}

    # --- request plumbing ---------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = {**self._auth_headers(), **kwargs.pop("headers", {})}
        for attempt in range(self.settings.servicenow_max_retries + 1):
            gap = time.time() - self._last_request_at
            if gap < self.settings.servicenow_min_request_interval_s:
                time.sleep(self.settings.servicenow_min_request_interval_s - gap)
            started = time.time()
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
                log.debug(
                    "   servicenow %s %s -> %s (%.1fs)",
                    method, _describe(url, kwargs.get("params")), response.status_code, time.time() - started,
                )
            except httpx.TransportError as exc:
                # Network blips (DNS failures, dropped connections) are usually
                # gone a second later; a ticket should not fail on one.
                if attempt >= self.settings.servicenow_max_retries:
                    raise ServiceNowError(f"could not reach ServiceNow after {attempt + 1} tries: {exc}") from exc
                delay = min(8.0, 0.5 * 2**attempt)
                log.warning("could not reach ServiceNow (%s), retrying in %.1fs", type(exc).__name__, delay)
                time.sleep(delay)
                continue
            self._last_request_at = time.time()
            if response.status_code in RETRYABLE_STATUS and attempt < self.settings.servicenow_max_retries:
                delay = float(response.headers.get("Retry-After") or 0) or min(8.0, 0.5 * 2**attempt)
                log.warning("ServiceNow %s %s -> %s, retrying in %.1fs", method, url, response.status_code, delay)
                time.sleep(delay + random.random() * 0.2)
                continue
            if response.status_code >= 400:
                hint = ERROR_HINTS.get(response.status_code, "")
                raise ServiceNowError(
                    f"ServiceNow {method} {url} failed with {response.status_code}"
                    f"{': ' + hint if hint else ''} | {response.text[:300]}"
                )
            return response
        raise ServiceNowError(f"{method} {url}: retries exhausted")

    # --- reads --------------------------------------------------------------

    def query_records(
        self, table: str, query: str, *, fields: list[str] | None = None, limit: int | None = None,
        page_size: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        page_size = page_size or self.settings.servicenow_page_size
        fields = fields or TABLE_FIELDS.get(table)
        offset = 0
        fetched = 0
        for _ in range(self.settings.servicenow_max_pages):
            params = {
                "sysparm_query": query,
                "sysparm_limit": str(page_size if limit is None else min(page_size, limit - fetched)),
                "sysparm_offset": str(offset),
                "sysparm_display_value": "all",
            }
            if fields:
                params["sysparm_fields"] = ",".join(fields)
            response = self._request("GET", f"/api/now/table/{table}", params=params)
            rows = response.json().get("result", [])
            if not rows:
                return
            for row in rows:
                yield row
                fetched += 1
                if limit is not None and fetched >= limit:
                    return
            if len(rows) < int(params["sysparm_limit"]):
                return
            offset += len(rows)

    def get_ticket(self, number: str, table: str = "incident", *, with_journal: bool = True) -> Ticket:
        rows = list(self.query_records(table, f"number={number}", limit=1))
        if not rows:
            raise ServiceNowError(f"{table} {number} not found")
        ticket = self.to_ticket(rows[0], table)
        if with_journal:
            ticket.journal = self.get_journal(table, [ticket.sys_id]).get(ticket.sys_id, [])
        return ticket

    def get_journal(
        self, table: str, sys_ids: list[str], elements: tuple[str, ...] = ("work_notes", "comments")
    ) -> dict[str, list[JournalEntry]]:
        """Reads notes through the ticket's own journal fields.

        The journal table itself is admin-only by default, and a non-admin account
        gets an empty result rather than an error. Reading the fields on the ticket
        works for any account that can read the ticket.
        """

        out: dict[str, list[JournalEntry]] = {sid: [] for sid in sys_ids}
        for start in range(0, len(sys_ids), 50):
            batch = [s for s in sys_ids[start : start + 50] if s]
            if not batch:
                continue
            rows = self.query_records(
                table, f"sys_idIN{','.join(batch)}", fields=["sys_id", *elements], page_size=50
            )
            for row in rows:
                sys_id = _value(row, "sys_id")
                entries = [entry for element in elements for entry in parse_journal(_display(row, element), element)]
                entries.sort(key=lambda e: e.created_on or datetime.min.replace(tzinfo=timezone.utc))
                out[sys_id] = entries
        return out

    def search_text(
        self, table: str, text: str, *, clauses: list[str] | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """ServiceNow's own full-text search (Zing), which indexes short
        description, description, work notes and close notes. Results come back
        in relevance order."""

        cleaned = _clean_search_text(text)
        if not cleaned:
            return []
        query = "^".join([f"{ZING_OPERATOR}={cleaned}", *(clauses or [])])
        return list(self.query_records(table, query, limit=limit))

    def search_by_fields(
        self,
        table: str,
        equals: dict[str, str],
        *,
        clauses: list[str] | None = None,
        limit: int = 20,
        order_by: str = "ORDERBYDESCopened_at",
    ) -> list[dict[str, Any]]:
        """Scoped sweep: every recent ticket on this configuration item or in this
        category. Catches the cases where the wording differs so much that
        full-text search finds nothing."""

        parts = [f"{field}={value}" for field, value in equals.items() if value]
        if not parts:
            return []
        query = "^".join([*parts, *(clauses or []), order_by])
        return list(self.query_records(table, query, limit=limit))

    # --- write --------------------------------------------------------------

    def post_work_note(self, table: str, sys_id: str, note: str) -> dict[str, Any]:
        """The only write this agent performs."""

        response = self._request(
            "PATCH",
            f"/api/now/table/{table}/{sys_id}",
            params={"sysparm_fields": "sys_id,number", "sysparm_display_value": "all"},
            json={"work_notes": note},
            headers={"Content-Type": "application/json"},
        )
        return response.json().get("result", {})

    # --- mapping ------------------------------------------------------------

    @staticmethod
    def to_ticket(record: dict[str, Any], table: str) -> Ticket:
        extra = {
            field: _value(record, field)
            for field in EXTRA_RESOLUTION_FIELDS.get(table, [])
            if _value(record, field).strip()
        }
        change_ids = [
            sid for sid in (_value(record, "rfc"), _value(record, "caused_by")) if sid
        ]
        reopen_raw = _value(record, "reopen_count")
        state = _value(record, "state")
        resolved_at = parse_sn_datetime(_value(record, "resolved_at") or _value(record, "closed_at"))
        active_flag = _value(record, "active").lower() in {"true", "1"}
        is_open = (
            table not in KNOWLEDGE_TABLES
            and active_flag
            and resolved_at is None
            and state not in CLOSED_INCIDENT_STATES
        )
        return Ticket(
            sys_id=_value(record, "sys_id"),
            number=_value(record, "number"),
            table=table,
            short_description=_value(record, "short_description"),
            description=_value(record, "description"),
            state=state,
            state_label=_display(record, "state"),
            active=is_open,
            priority=_display(record, "priority"),
            category=_value(record, "category"),
            subcategory=_value(record, "subcategory"),
            cmdb_ci=_display(record, "cmdb_ci"),
            cmdb_ci_id=_value(record, "cmdb_ci"),
            business_service=_value(record, "business_service"),
            assignment_group=_display(record, "assignment_group"),
            assigned_to=_display(record, "assigned_to"),
            resolved_by=_display(record, "resolved_by"),
            opened_at=parse_sn_datetime(_value(record, "opened_at")),
            resolved_at=resolved_at,
            updated_on=parse_sn_datetime(_value(record, "sys_updated_on")),
            close_code=_display(record, "close_code"),
            close_notes=_value(record, "close_notes"),
            extra_resolution=extra,
            reopen_count=int(reopen_raw) if reopen_raw.isdigit() else 0,
            problem_id=_value(record, "problem_id"),
            change_ids=change_ids,
        )
