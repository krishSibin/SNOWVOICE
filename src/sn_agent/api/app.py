"""Webhook that ServiceNow calls, plus the workers and the sweep.

Two ways in, one queue. The webhook reacts the moment a ticket is created or
updated; the sweep periodically asks ServiceNow for open tickets that still lack
an agent note, which catches anything the webhook never saw. Both feed the same
priority queue, so a P1 is always worked before a P4.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from ..config import get_settings
from ..runner import JobRunner, Submit
from ..servicenow.client import ServiceNowClient
from ..sweep import Sweeper

log = logging.getLogger(__name__)

runner = JobRunner(
    workers=get_settings().worker_threads, max_failures=get_settings().max_failures_per_ticket
)


@asynccontextmanager
async def lifespan(_app: FastAPI):  # pragma: no cover - server lifecycle
    settings = get_settings()
    runner.start()
    if settings.sweep_enabled:
        Sweeper(runner, settings, lambda: ServiceNowClient(settings)).start()
    yield


app = FastAPI(title="ServiceNow Resolution Agent", lifespan=lifespan)


def _verify(raw_body: bytes, signature: str | None) -> None:
    secret = get_settings().webhook_secret.get_secret_value()
    if not secret:
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not signature:
        raise HTTPException(status_code=401, detail="missing signature")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise HTTPException(status_code=401, detail="bad signature")


@app.get("/health")
def health() -> dict[str, object]:
    return {"status": "ok", "queued": runner.queue.qsize(), "in_flight": len(runner.in_flight)}


@app.post("/webhook/servicenow", status_code=202)
async def servicenow_webhook(request: Request, x_sn_signature: str | None = Header(default=None)) -> dict:
    if not get_settings().webhook_enabled:
        # Behaves as if the endpoint does not exist, so a scan learns nothing.
        raise HTTPException(status_code=404)
    raw = await request.body()
    _verify(raw, x_sn_signature)
    payload = await request.json()
    number = str(payload.get("number") or "").strip()
    if not number:
        raise HTTPException(status_code=400, detail="number is required")
    table = str(payload.get("table") or "incident")
    priority = _priority(payload.get("priority"))

    status = runner.submit(number, table, priority)
    if status == Submit.FULL:
        raise HTTPException(status_code=503, detail="queue full, retry later")
    return {"number": number, "priority": priority, "status": status.value}


def _priority(value: object) -> int:
    """ServiceNow sends 1 (critical) to 5 (planning). Anything unreadable is
    treated as middling rather than guessed at either extreme."""

    try:
        rank = int(str(value).strip()[:1])
    except (TypeError, ValueError):
        return 3
    return rank if 1 <= rank <= 5 else 3
