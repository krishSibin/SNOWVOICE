"""Logging and tracing.

Several tickets are worked at once, so every log line carries the ticket it
belongs to:

    14:02:11 INFO    [INC0010006] searched ServiceNow: 8 candidate tickets
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from opentelemetry import trace

from .config import Settings

_tracer = trace.get_tracer("sn_agent")
_current_ticket: ContextVar[str] = ContextVar("current_ticket", default="")

# Libraries that log every HTTP request with its full URL, or repeat the same
# notice on every call. Our own code logs the same requests in a short form
# with --verbose, so these only ever show real problems.
NOISY_LOGGERS = ("httpx", "httpcore", "openai", "urllib3", "uvicorn.access")

LEVEL_COLOURS = {
    logging.DEBUG: "\033[90m",  # grey
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",
}
RESET = "\033[0m"


def get_tracer():
    return _tracer


@contextmanager
def ticket_context(number: str) -> Iterator[None]:
    """Tags every log line inside the block with this ticket number. Each worker
    thread has its own context, so parallel tickets never mix their tags."""

    token = _current_ticket.set(number)
    try:
        yield
    finally:
        _current_ticket.reset(token)


class _TicketFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.ticket = _current_ticket.get()
        return True


class _Formatter(logging.Formatter):
    def __init__(self, colour: bool):
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        ticket = getattr(record, "ticket", "")
        tag = f"[{ticket}] " if ticket else ""
        line = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {tag}{record.getMessage()}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        colour = LEVEL_COLOURS.get(record.levelno) if self.colour else None
        return f"{colour}{line}{RESET}" if colour else line


def setup(settings: Settings, *, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(_TicketFilter())
    handler.setFormatter(_Formatter(colour=_supports_colour(sys.stderr)))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)

    if settings.otel_exporter == "none":
        return
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": "sn-agent"}))
    if settings.otel_exporter == "console":
        exporter = ConsoleSpanExporter()
    else:  # pragma: no cover - needs the optional OTLP package and a collector
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def _supports_colour(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if os.name == "nt":
        # Turns on ANSI colour handling in the Windows console.
        os.system("")
    return True
