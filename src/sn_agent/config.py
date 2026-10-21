from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Price per 1M tokens (input, output) in USD. The TCS GenAI Lab MaaS gateway is
# provided for the hackathon at no per-token cost to the participant, so these
# are 0.0 by default; override with PRICES_JSON if that ever changes. Kept for
# budget enforcement and cost reporting, never for routing decisions.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "azure/genailab-maas-gpt-35-turbo": (0.0, 0.0),
    "azure/genailab-maas-gpt-4o": (0.0, 0.0),
    "azure/genailab-maas-gpt-4o-mini": (0.0, 0.0),
    "azure_ai/genailab-maas-DeepSeek-R1": (0.0, 0.0),
    "azure_ai/genailab-maas-DeepSeek-V3-0324": (0.0, 0.0),
    "azure_ai/genailab-maas-Llama-3.2-90B-Vision-Instruct": (0.0, 0.0),
    "azure_ai/genailab-maas-Llama-3.3-70B-Instruct": (0.0, 0.0),
    "azure_ai/genailab-maas-Llama-4-Maverick-17B-128E-Instruct-FP8": (0.0, 0.0),
    "azure_ai/genailab-maas-Phi-3.5-vision-instruct": (0.0, 0.0),
    "azure_ai/genailab-maas-Phi-4-reasoning": (0.0, 0.0),
}

# Weights for the confidence signals. Hand-tuned; CONFIDENCE_WEIGHTS_FILE can
# override them once evaluation runs show what the signals are worth.
DEFAULT_WEIGHTS: dict[str, float] = {
    "retrieval_strength": 0.25,
    "corroboration": 0.20,
    "citation_support": 0.30,
    "source_quality": 0.15,
    "agreement": 0.05,
    "self_rating": 0.05,
}


class Settings(BaseSettings):
    # env_ignore_empty: a line like `MAX_ITERATIONS=` keeps the default below,
    # so .env.example can list every setting without values.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", protected_namespaces=(), env_ignore_empty=True
    )

    # ServiceNow
    servicenow_instance_url: str = ""
    servicenow_auth_mode: Literal["oauth", "basic"] = "basic"
    servicenow_client_id: str = ""
    servicenow_client_secret: SecretStr = SecretStr("")
    servicenow_username: str = ""
    servicenow_password: SecretStr = SecretStr("")
    servicenow_timeout_s: float = 30.0
    servicenow_min_request_interval_s: float = 0.05
    servicenow_max_retries: int = 4

    # TCS GenAI Lab (only model source allowed for the AI Fridays hackathon)
    tcs_genai_base_url: str = "https://genailab.tcs.in"
    tcs_genai_api_key: SecretStr = SecretStr("")
    # Verification runs against the Windows certificate store, which holds the
    # corporate CA that re-signs HTTPS here — the bundled certifi roots do not,
    # which is why the handbook's sample code resorts to verify=False. Set this
    # false only on a machine whose store is missing that CA.
    tcs_genai_verify_ssl: bool = True
    model_lite: str = "azure/genailab-maas-gpt-4o-mini"
    model_flash: str = "azure/genailab-maas-gpt-4o"
    # The handbook lists more models than the gateway actually serves: both
    # DeepSeek routes, Llama-3.3-70B and Phi-4 all fail there. Maverick is the
    # one working non-OpenAI model, so escalation gets a second opinion from a
    # different model family rather than re-rolling the same one.
    model_pro: str = "azure_ai/genailab-maas-Llama-4-Maverick-17B-128E-Instruct-FP8"
    llm_max_retries: int = 3

    servicenow_page_size: int = 100  # records per Table API page
    servicenow_max_pages: int = 100

    # Search. Everything is queried live from ServiceNow; nothing is stored.
    search_results_per_query: int = 10
    search_sweep_limit: int = 15
    max_candidate_tickets: int = 12
    chunk_max_chars: int = 1500
    search_recency_days: int = 1095
    # Where past tickets are searched. "live" asks ServiceNow on every search;
    # "index" uses the local copy `sn-agent index sync` keeps in APP_DB_PATH,
    # which is faster and matches by any word; "hybrid" uses the index and
    # falls back to live when it finds nothing. Open tickets are always live.
    knowledge_source: Literal["live", "index", "hybrid"] = "live"

    # Open-ticket detection
    open_ticket_window_days: int = 14
    open_ticket_queries: int = 2
    open_ticket_max_candidates: int = 5
    major_incident_min_tickets: int = 3
    major_incident_window_hours: int = 24

    # Loop and budget
    max_iterations: int = 3
    max_cost_usd_per_ticket: float = 0.25
    max_tokens_per_ticket: int = 400_000

    # Confidence
    confidence_high: float = 75.0
    confidence_medium: float = 50.0
    agreement_samples: int = 2
    agreement_band: float = 10.0
    confidence_weights_file: str = ""
    min_support_rate_for_high: float = 0.5

    # Webhook. Off by default: with it off the agent only ever makes outbound
    # calls, so there is no open port for anyone to attack. Turn it on only
    # behind HTTPS with an IP allowlist.
    webhook_enabled: bool = False
    webhook_secret: SecretStr = SecretStr("")

    # Intake agent: the chat/voice front door that tries to fix an issue before
    # a ticket exists. It creates incidents through its own ServiceNow account,
    # so the ticket agent's account stays read-and-work-notes only.
    sn_intake_username: str = ""
    sn_intake_password: SecretStr = SecretStr("")
    # Off: the agent reads back the incident it would raise and logs the exact
    # payload, but creates nothing. Turn on only once the payload looks right.
    intake_create_tickets: bool = False
    intake_default_group: str = "Service Desk"
    intake_correlation_id: str = "sn-intake-agent"
    # Display names of accounts whose work notes must never count as evidence
    # (the ticket agent's own account), comma separated. Notes carrying the
    # ticket agent's header are always excluded, whoever posted them.
    intake_agent_note_authors: str = ""
    intake_max_candidates: int = 8
    intake_search_threads: int = 4
    # Model for conversation turns. Defaults to MODEL_LITE: speed matters more
    # than depth while someone is waiting on the line.
    intake_chat_model: str = ""

    # Web app: the end-user chatbot page and the ops console.
    app_db_path: str = "data/sn_agent.db"
    app_secret_key: SecretStr = SecretStr("")
    # One standing login per role, created (or its password reset) at startup
    # so a fresh database can be signed in to straight away. Leave blank to
    # manage logins only with `sn-agent users`.
    app_user_email: str = ""
    app_user_password: SecretStr = SecretStr("")
    app_admin_email: str = ""
    app_admin_password: SecretStr = SecretStr("")
    record_agent_runs: bool = True

    # LiveKit. The defaults match `livekit-server --dev` on this machine.
    livekit_url: str = "ws://127.0.0.1:7880"
    livekit_public_url: str = ""  # what browsers connect to, if different
    livekit_api_key: str = "devkey"
    livekit_api_secret: SecretStr = SecretStr("secret")

    # Local speech models (no external API): faster-whisper and Kokoro.
    stt_model: str = "distil-small.en"
    # "local" runs faster-whisper here; "gateway" sends each phrase to the TCS
    # GenAI Lab whisper (more accurate, adds a network round trip per phrase).
    stt_provider: Literal["local", "gateway"] = "local"
    stt_gateway_model: str = "azure/genailab-maas-whisper"
    stt_device: str = "auto"  # auto, cpu or cuda
    stt_language: str = "en"
    tts_model_path: str = "models/kokoro-v1.0.int8.onnx"
    tts_voices_path: str = "models/voices-v1.0.bin"
    tts_voice: str = "af_heart"
    tts_speed: float = 1.05
    # Mutes microphone audio that is not clearly louder than the room, so
    # voices across the room and background noise are never heard as speech.
    noise_gate_enabled: bool = True
    noise_gate_min_db: float = -45.0  # audio quieter than this never counts
    noise_gate_margin_db: float = 12.0  # how far above the room's background speech must be
    noise_gate_relative_db: float = 15.0  # mute what is this far below the caller's own voice
    # How sure the voice detector must be that a sound is speech before a turn
    # starts (0-1). 0.7 ignores office chatter; lower it (e.g. 0.5) if a quiet
    # speaker's own voice is not picked up.
    vad_activation_threshold: float = 0.7
    # Seconds the voice agent waits for the model to start answering before it
    # retries. Raise it if the gateway is slow to start answering.
    voice_llm_timeout_s: float = 10.0

    # Queue and sweep. ServiceNow is the queue: the sweep asks it which open
    # tickets still lack an agent note, and workers take them most urgent first.
    worker_threads: int = 3
    sweep_enabled: bool = True
    sweep_interval_minutes: float = 1.0
    sweep_window_days: int = 3  # only tickets opened recently, so a backlog can't run up a bill
    sweep_max_tickets: int = 50
    max_failures_per_ticket: int = 3

    # Observability
    log_level: str = "INFO"
    otel_exporter: Literal["none", "console", "otlp"] = "none"

    prices_json: str = Field(default="", description="JSON override for model prices")

    @property
    def prices(self) -> dict[str, tuple[float, float]]:
        prices = dict(DEFAULT_PRICES)
        if self.prices_json:
            for model, pair in json.loads(self.prices_json).items():
                prices[model] = (float(pair[0]), float(pair[1]))
        return prices

    @property
    def confidence_weights(self) -> dict[str, float]:
        if self.confidence_weights_file:
            path = Path(self.confidence_weights_file)
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return {k: float(v) for k, v in data["weights"].items()}
        return dict(DEFAULT_WEIGHTS)


@lru_cache
def get_settings() -> Settings:
    return Settings()
