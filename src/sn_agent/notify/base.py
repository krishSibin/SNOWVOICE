from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from ..agent.schemas import RunResult


class PublishResult(BaseModel):
    channel: str
    delivered: bool = False
    detail: str = ""
    body: str = ""


class Notifier(Protocol):
    """v1 ships the ServiceNow work note. Email to the assignment group and Teams
    slot in here later without the pipeline changing."""

    channel: str

    def publish(
        self, result: RunResult, *, previous_run_id: str = "", fingerprint: str = ""
    ) -> PublishResult: ...
