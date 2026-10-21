"""Publishes what `sn-agent serve` is doing, for the ops console's live view.

`serve` and `sn-agent web` are separate processes that share only the SQLite
file, so the running agent writes its state there:

- a heartbeat every few seconds: alive or not, what each worker is on, the
  queue, the last and next sweep, and the tickets that still need the agent;
- one row per finished ticket, including failures, which are otherwise only
  in the terminal;
- every log line, through the step log, including the sweep's own.

Like the recorder, it only observes: if writing fails, the agent carries on.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import asdict

from .config import Settings
from .recorder import install_step_log, open_store
from .runner import JobResult, JobRunner
from .store import Store

log = logging.getLogger(__name__)

HEARTBEAT_S = 3.0


class AgentMonitor:
    def __init__(
        self, runner: JobRunner, settings: Settings, sweeper=None, *, mode: str = "serve", store: Store | None = None
    ):
        self.runner = runner
        self.sweeper = sweeper
        self.settings = settings
        self.mode = mode
        self.store = store or open_store(settings)
        self.started_at = time.time()
        self._stop = threading.Event()
        install_step_log(self.store, untagged=True)
        runner.on_result = self.record

    def record(self, result: JobResult) -> None:
        self.store.add_job({**asdict(result), "at": time.time()})

    def start(self) -> "AgentMonitor":
        self.beat()
        threading.Thread(target=self._loop, name="sn-agent-monitor", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.beat(stopped=True)

    def _loop(self) -> None:  # pragma: no cover - timing loop
        while not self._stop.wait(HEARTBEAT_S):
            self.beat()

    def beat(self, *, stopped: bool = False) -> None:
        settings = self.settings
        try:
            self.store.put_agent_status(
                {
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "mode": self.mode,
                    "started_at": self.started_at,
                    "stopped": stopped,
                    "runner": self.runner.snapshot(),
                    "sweep": self.sweeper.snapshot() if self.sweeper is not None else None,
                    "config": {
                        "models": [settings.model_lite, settings.model_flash, settings.model_pro],
                        "knowledge_source": settings.knowledge_source,
                        "max_failures": settings.max_failures_per_ticket,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001 - the console is optional, the agent is not
            log.debug("could not write the agent status: %s", exc)
