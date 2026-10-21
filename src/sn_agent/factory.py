"""Builds the production objects from settings. Tests assemble their own."""

from __future__ import annotations

from .agent.pipeline import ResolutionPipeline
from .config import Settings, get_settings
from .llm import TcsGenAiLLM
from .notify.servicenow_note import ServiceNowWorkNoteNotifier
from .servicenow.client import ServiceNowClient
from .service import ResolutionService
from .sources.servicenow_search import ServiceNowSearchSource
from .sources.ticket_index import knowledge_source


def build_service(settings: Settings | None = None, *, dry_run: bool = False) -> ResolutionService:
    settings = settings or get_settings()
    llm = TcsGenAiLLM(settings)
    client = ServiceNowClient(settings)
    source = knowledge_source(settings, ServiceNowSearchSource(client, settings, llm))
    pipeline = ResolutionPipeline(settings, llm, source)
    notifier = ServiceNowWorkNoteNotifier(client, dry_run=dry_run)
    if settings.record_agent_runs:
        # Observes the run for the ops console; the agent's behaviour is unchanged.
        from .recorder import RecordingNotifier, install_step_log, open_store

        store = open_store(settings)
        install_step_log(store)
        notifier = RecordingNotifier(notifier, store)
    return ResolutionService(
        settings,
        client=client,
        pipeline=pipeline,
        notifier=notifier,
    )
