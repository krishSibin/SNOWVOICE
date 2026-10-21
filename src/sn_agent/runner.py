"""The work queue: most urgent ticket first, several at a time.

It lives in memory on purpose. Nothing about a ticket is stored outside
ServiceNow, so this queue is only a to-do list for the current process: if the
process restarts, the next sweep rebuilds it from ServiceNow.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .observability import ticket_context

log = logging.getLogger(__name__)


class Submit(str, Enum):
    QUEUED = "queued"
    ALREADY_QUEUED = "already queued"
    GAVE_UP = "gave up after repeated failures"
    FULL = "queue full"


@dataclass
class JobResult:
    number: str
    priority: int
    status: str  # posted | printed | skipped | failed
    detail: str = ""
    outcome: str = ""
    confidence: float | None = None
    cost_usd: float = 0.0


class JobRunner:
    def __init__(
        self,
        *,
        workers: int = 3,
        service_factory: Callable[[], object] | None = None,
        max_failures: int = 3,
        maxsize: int = 1000,
        on_result: Callable[[JobResult], None] | None = None,
    ):
        self.queue: queue.PriorityQueue = queue.PriorityQueue(maxsize=maxsize)
        self.workers = workers
        self.max_failures = max_failures
        self.service_factory = service_factory or _default_service
        self.in_flight: set[str] = set()
        self.failures: dict[str, int] = {}
        self.results: list[JobResult] = []
        self.on_result = on_result
        self.working: dict[str, float] = {}  # ticket number -> when a worker picked it up
        self.lock = threading.Lock()
        self._sequence = itertools.count()
        self._started = False

    def start(self) -> None:
        with self.lock:
            if self._started:
                return
            self._started = True
        for index in range(self.workers):
            threading.Thread(target=self._loop, name=f"sn-agent-worker-{index}", daemon=True).start()
        log.info("started %d worker(s)", self.workers)

    def submit(self, number: str, table: str = "incident", priority: int = 3) -> Submit:
        with self.lock:
            if number in self.in_flight:
                return Submit.ALREADY_QUEUED
            if self.failures.get(number, 0) >= self.max_failures:
                # A ticket that keeps failing would otherwise be retried by every
                # sweep, forever, and cost money each time.
                return Submit.GAVE_UP
            self.in_flight.add(number)
        try:
            # The sequence number keeps equal priorities first-in, first-out.
            self.queue.put_nowait((priority, next(self._sequence), number, table))
        except queue.Full:
            with self.lock:
                self.in_flight.discard(number)
            return Submit.FULL
        return Submit.QUEUED

    def wait(self) -> None:
        self.queue.join()

    def _loop(self) -> None:
        service = self.service_factory()
        while True:
            priority, _seq, number, table = self.queue.get()
            with self.lock:
                self.working[number] = time.time()
            try:
                with ticket_context(number):
                    log.info("picked up (P%d); %d more waiting in the queue", priority, self.queue.qsize())
                    self._record(self._process(service, number, table, priority))
            finally:
                with self.lock:
                    self.in_flight.discard(number)
                    self.working.pop(number, None)
                self.queue.task_done()

    def _process(self, service, number: str, table: str, priority: int) -> JobResult:
        try:
            outcome = service.resolve(number, table)
        except Exception as exc:
            with self.lock:
                self.failures[number] = self.failures.get(number, 0) + 1
                attempts = self.failures[number]
            log.exception(
                "FAILED (attempt %d of %d): %s", attempts, self.max_failures, exc,
            )
            if attempts >= self.max_failures:
                log.error("giving up on this ticket until the agent restarts")
            return JobResult(number, priority, "failed", detail=str(exc)[:200])

        with self.lock:
            self.failures.pop(number, None)
        if outcome.skipped_reason:
            return JobResult(number, priority, "skipped", detail=outcome.skipped_reason)
        result = outcome.result
        return JobResult(
            number,
            priority,
            "posted" if outcome.published.delivered else "printed",
            outcome=result.outcome.value,
            confidence=result.confidence.score,
            cost_usd=result.cost_usd,
        )

    def snapshot(self) -> dict:
        """What the workers are doing now, for the ops console."""

        with self.queue.mutex:
            waiting = sorted(self.queue.queue)
        with self.lock:
            working = dict(self.working)
            gave_up = sorted(n for n, count in self.failures.items() if count >= self.max_failures)
            totals: dict[str, int] = {}
            for result in self.results:
                totals[result.status] = totals.get(result.status, 0) + 1
        return {
            "workers": self.workers,
            "working": [{"number": n, "since": t} for n, t in sorted(working.items(), key=lambda kv: kv[1])],
            "queued": [{"number": number, "priority": priority} for priority, _seq, number, _table in waiting],
            "gave_up": gave_up,
            "totals": totals,
        }

    def _record(self, result: JobResult) -> None:
        with self.lock:
            self.results.append(result)
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception as exc:  # noqa: BLE001 - recording must never stop a worker
                log.warning("could not record the result for the ops console: %s", exc)
        if result.status in {"posted", "printed"}:
            log.info(
                "finished: %s, %s, confidence %.0f/100, $%.4f",
                result.status, result.outcome, result.confidence or 0.0, result.cost_usd,
            )
        elif result.status == "skipped":
            log.info("finished: skipped (%s)", result.detail)


def _default_service():
    from .factory import build_service

    return build_service()
