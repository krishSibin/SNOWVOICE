"""Records the ticket agent's runs for the ops console, without changing what
the agent does.

Two pieces, both attached from the outside:

- `RecordingNotifier` wraps the work-note notifier and keeps each finished
  run's result (outcome, confidence, citations, trace) after the note is sent.
- `StepLog` is a logging handler that keeps the agent's own step-by-step log
  lines, tagged with the ticket they belong to.

If recording fails, the agent carries on: the note matters, the record does not.
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache

from .config import Settings
from .observability import _current_ticket
from .store import Store

log = logging.getLogger(__name__)


@lru_cache(maxsize=4)
def _store_at(path: str) -> Store:
    return Store(path)


def open_store(settings: Settings) -> Store:
    return _store_at(settings.app_db_path)


class RecordingNotifier:
    def __init__(self, inner, store: Store):
        self.inner = inner
        self.store = store
        self.channel = inner.channel

    def publish(self, result, *, previous_run_id: str = "", fingerprint: str = ""):
        published = self.inner.publish(result, previous_run_id=previous_run_id, fingerprint=fingerprint)
        try:
            record = result.model_dump(mode="json")
            record["note"] = published.body
            self.store.save_run(record, delivered=published.delivered)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record run %s for the ops console: %s", result.run_id, exc)
        return published


class StepLog(logging.Handler):
    """Keeps INFO and above from the agent's loggers, for lines logged while a
    ticket is being worked. With `untagged`, also the lines that belong to no
    ticket (the sweep, the workers starting), for the live log of `serve`."""

    def __init__(self, store: Store, *, untagged: bool = False):
        super().__init__(level=logging.INFO)
        self.store = store
        self.untagged = untagged

    def emit(self, record: logging.LogRecord) -> None:
        ticket = getattr(record, "ticket", "") or _current_ticket.get()
        if (not ticket and not self.untagged) or record.name.startswith("sn_agent.recorder"):
            return
        try:
            self.store.add_step(ticket, record.levelname, record.getMessage(), at=record.created)
        except Exception:  # noqa: BLE001 - never let logging break a run
            pass


_installed: dict[object, StepLog] = {}
_install_lock = threading.Lock()


def install_step_log(store: Store, *, untagged: bool = False) -> None:
    """Idempotent: one handler per database, however many services are built.
    Asking for `untagged` once keeps untagged lines from then on."""

    # Every in-memory database is called ":memory:", but each is its own.
    key: object = id(store) if store.path == ":memory:" else store.path
    with _install_lock:
        handler = _installed.get(key)
        if handler is None:
            handler = _installed[key] = StepLog(store, untagged=untagged)
            logging.getLogger("sn_agent").addHandler(handler)
        handler.untagged = handler.untagged or untagged
