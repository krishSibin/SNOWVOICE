"""The voice (and in-room text) channel: a LiveKit agent worker.

One worker process can hold several conversations. Each browser that joins a
room gets its own `IntakeSession`, so voice and text share the same tools,
rules and records as the text-only channel.

Latency, end to end:
- Silero cuts the audio at a short pause, and that pause ends the turn. In a
  noisy room, where there is never a pause, the turn ends once the words have
  stopped changing (see `LiveWhisperSTT`). (The
  model-based turn detector needs LiveKit's cloud or a separate download, and
  on a laptop CPU it added seconds when it was missing.)
- The reply is drafted and its first sentence synthesized while the turn is
  still being confirmed (preemptive generation), streams from the gateway, and
  the first sentence is spoken while the rest is still being written.
- The user can interrupt at any time by talking, typing, or pressing Stop on
  the page, which calls the `stop_speaking` RPC below.

Run with:  sn-agent voice dev    (or `start` in production)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Literal

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    room_io,
)
from livekit.agents import tts as lk_tts
from livekit.agents.metrics import EOUMetrics, LLMMetrics, TTSMetrics
from livekit.plugins import openai as lk_openai
from livekit.plugins import silero

from ..config import get_settings
from ..observability import setup
from ..recorder import open_store
from .chat import chat_client, chat_model
from .noise_gate import NoiseGate
from .local_speech import IT_VOCABULARY, ClauseTokenizer, GatewaySTT, KokoroTTS, LiveWhisperSTT, WhisperSTT
from .prompts import CONVERSATION_STYLE_VOICE, CONVERSATION_SYSTEM, GREETING
from .session import IntakeSession, build_services

log = logging.getLogger(__name__)

Category = Literal["network", "software", "hardware", "database", "inquiry"]
AGENT_NAME = "intake-agent"


class IntakeVoiceAgent(Agent):
    def __init__(self, intake: IntakeSession):
        super().__init__(instructions=f"{CONVERSATION_SYSTEM}\n\n{CONVERSATION_STYLE_VOICE}")
        self.intake = intake

    @function_tool
    async def search_past_fixes(
        self,
        context: RunContext,
        summary: str,
        system: str,
        error_text: str = "",
        details: str = "",
        category: Category | None = None,
    ) -> str:
        """Search resolved past tickets for a documented fix to the user's issue. Call as soon as
        you know the symptom and the system. Returns only steps that past tickets document.

        Args:
            summary: One-line summary in an engineer's words
            system: Application, device or service affected
            error_text: Exact error message or code, if any
            details: Other relevant details the user gave
            category: Best-fitting category
        """
        return await self.intake.call_tool(
            "search_past_fixes",
            {"summary": summary, "system": system, "error_text": error_text, "details": details, "category": category},
        )

    @function_tool
    async def prepare_incident(
        self,
        context: RunContext,
        short_description: str,
        details: str,
        users_affected: Literal["just_me", "team", "many"],
        work_impact: Literal["cannot_work", "degraded", "minor"],
        security_related: bool = False,
        steps_tried: str = "",
        category: Category | None = None,
    ) -> str:
        """Work out the priority and support team for a new incident and return them for you to
        read back to the user. Creates nothing.

        Args:
            short_description: One line, under 100 characters
            details: What the user reported, in full
            users_affected: Just the user, their team, or many people
            work_impact: Whether the user can still work
            security_related: Phishing, malware or account compromise
            steps_tried: What the user already tried, including steps from this conversation
            category: Best-fitting category
        """
        return await self.intake.call_tool(
            "prepare_incident",
            {
                "short_description": short_description,
                "details": details,
                "users_affected": users_affected,
                "work_impact": work_impact,
                "security_related": security_related,
                "steps_tried": steps_tried,
                "category": category,
            },
        )

    @function_tool
    async def create_incident(self, context: RunContext) -> str:
        """Raise the incident prepared by prepare_incident. Only after the user said yes to the read-back."""
        return await self.intake.call_tool("create_incident", {})

    @function_tool
    async def mark_solved(self, context: RunContext, note: str = "") -> str:
        """Record that the issue is fixed and no ticket is needed. Call when the user confirms it works.

        Args:
            note: What fixed it
        """
        return await self.intake.call_tool("mark_solved", {"note": note})


def _stt(settings, vad_model):
    if settings.stt_provider == "gateway":
        log.info("speech-to-text: %s on the TCS GenAI Lab gateway", settings.stt_gateway_model)
        return GatewaySTT(
            lk_openai.STT(
                model=settings.stt_gateway_model,
                client=chat_client(settings),
                language=settings.stt_language,
                prompt=IT_VOCABULARY,
                use_realtime=False,
            )
        )
    whisper = WhisperSTT(settings.stt_model, device=settings.stt_device, language=settings.stt_language)
    return LiveWhisperSTT(whisper, vad_model, language=settings.stt_language)


def prewarm(proc: JobProcess) -> None:
    """Loads every model once per worker process, before any call arrives."""

    settings = get_settings()
    setup(settings)
    # A 0.45 s pause ends a phrase; the default 0.55 s felt sluggish. In an
    # office, a stricter threshold and a 0.25 s minimum keep keyboard clicks,
    # coughs and voices across the room from counting as speech.
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=0.45, min_speech_duration=0.25, activation_threshold=0.7
    )
    proc.userdata["stt"] = _stt(settings, proc.userdata["vad"])
    # Long sentences are spoken clause by clause, so the first words play sooner.
    proc.userdata["tts"] = lk_tts.StreamAdapter(
        tts=KokoroTTS(
            settings.tts_model_path, settings.tts_voices_path, voice=settings.tts_voice, speed=settings.tts_speed
        ),
        sentence_tokenizer=ClauseTokenizer(),
    )
    services = build_services(settings, store=open_store(settings))
    services.warm()
    proc.userdata["services"] = services


async def entrypoint(ctx: JobContext) -> None:
    settings = get_settings()
    await ctx.connect()
    participant = await ctx.wait_for_participant()
    try:
        meta = json.loads(participant.metadata or "{}")
    except json.JSONDecodeError:
        meta = {}

    services = ctx.proc.userdata["services"]
    intake = IntakeSession(
        services,
        user_email=str(meta.get("email", "")),
        user_name=str(meta.get("name", "")),
        channel="voice",
        session_id=str(meta.get("conversation_id") or "") or None,
    )
    log.info("conversation %s started with %s", intake.id, intake.user_email or participant.identity)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=ctx.proc.userdata["stt"],
        tts=ctx.proc.userdata["tts"],
        llm=lk_openai.LLM(model=chat_model(settings), client=chat_client(settings), temperature=0.3),
        turn_handling={
            # "stt": the speech-to-text stream says when the turn ends. It ends
            # at a pause, like the voice detector, and also when the words stop
            # though the sound goes on, which in a noisy room it never stops.
            "turn_detection": "stt" if settings.stt_provider == "local" else "vad",
            "endpointing": {"min_delay": 0.3, "max_delay": 1.5},
            # "vad" here: left to choose, the session picks the adaptive detector,
            # which runs on LiveKit's cloud; without it, talking over the agent
            # did nothing. min_words: background with no words in it can't cut
            # the reply off; saying anything still does.
            "interruption": {"enabled": True, "mode": "vad", "min_duration": 0.6, "min_words": 1},
            "preemptive_generation": {"enabled": True, "preemptive_tts": True},
        },
        max_tool_steps=4,
    )

    @session.on("conversation_item_added")
    def _on_item(event) -> None:
        item = event.item
        role = getattr(item, "role", "")
        text = getattr(item, "text_content", None)
        if role in {"user", "assistant"} and text:
            intake.add_message(role, text)

    # Time from the user stopping to the first audio of the reply, per turn.
    pending: dict[str, dict[str, float]] = {}

    @session.on("metrics_collected")
    def _on_metrics(event) -> None:
        metric = event.metrics
        speech_id = getattr(metric, "speech_id", None)
        if not speech_id:
            return
        parts = pending.setdefault(speech_id, {})
        # A reply cut off before it spoke never completes; forget the oldest.
        while len(pending) > 8:
            pending.pop(next(iter(pending)))
        if isinstance(metric, EOUMetrics):
            parts["eou"] = metric.end_of_utterance_delay
            parts["stt"] = metric.transcription_delay
        elif isinstance(metric, LLMMetrics):
            parts["llm"] = metric.ttft
        elif isinstance(metric, TTSMetrics):
            parts.setdefault("tts", metric.ttfb)
        if {"eou", "llm", "tts"} <= parts.keys():
            total = parts["eou"] + parts["llm"] + parts["tts"]
            intake.record_turn_latency(total)
            log.info(
                "turn latency %.2fs (end of turn %.2f, of which transcription %.2f"
                " + first token %.2f + first audio %.2f)",
                total, parts["eou"], parts["stt"], parts["llm"], parts["tts"],
            )
            pending.pop(speech_id, None)

    async def _closed() -> None:
        intake.close()
        log.info("conversation %s ended: %s", intake.id, intake.outcome)

    ctx.add_shutdown_callback(_closed)
    started = time.time()
    # Background voices and noise are muted before anything listens.
    audio_input = room_io.AudioInputOptions(
        noise_cancellation=(
            NoiseGate(
                min_db=settings.noise_gate_min_db,
                margin_db=settings.noise_gate_margin_db,
                relative_db=settings.noise_gate_relative_db,
            )
            if settings.noise_gate_enabled
            else None
        ),
        auto_gain_control=False,  # boosting a quiet room would lift the background over the gate
    )
    await session.start(
        agent=IntakeVoiceAgent(intake), room=ctx.room, room_options=room_io.RoomOptions(audio_input=audio_input)
    )

    async def _stop_speaking(_data) -> str:
        """The page's Stop button: cut the reply off now, tool call or not."""
        await session.interrupt(force=True)
        log.info("conversation %s: user stopped the reply", intake.id)
        return "ok"

    ctx.room.local_participant.register_rpc_method("stop_speaking", _stop_speaking)

    # Hold to talk, for noisy rooms: the page switches it on, and then nothing
    # the microphone picks up is heard except while the button is held.
    async def _push_to_talk(data) -> str:
        on = data.payload == "on"
        automatic = "stt" if settings.stt_provider == "local" else "vad"
        session.update_options(turn_detection="manual" if on else automatic)
        session.clear_user_turn()
        session.input.set_audio_enabled(not on)
        log.info("conversation %s: hold to talk %s", intake.id, "on" if on else "off")
        return "ok"

    async def _start_turn(_data) -> str:
        await session.interrupt(force=True)
        session.clear_user_turn()
        session.input.set_audio_enabled(True)
        return "ok"

    async def _end_turn(_data) -> str:
        session.input.set_audio_enabled(False)
        # Whisper finishes the phrase after the call returns; the reply follows.
        done = session.commit_user_turn(transcript_timeout=10.0, stt_flush_duration=1.0)
        done.add_done_callback(lambda f: f.cancelled() or f.exception())
        return "ok"

    async def _cancel_turn(_data) -> str:
        session.input.set_audio_enabled(False)
        session.clear_user_turn()
        return "ok"

    for name, handler in {
        "set_push_to_talk": _push_to_talk,
        "start_turn": _start_turn,
        "end_turn": _end_turn,
        "cancel_turn": _cancel_turn,
    }.items():
        ctx.room.local_participant.register_rpc_method(name, handler)
    session.say(GREETING)
    log.info("session ready in %.1fs", time.time() - started)


def run(argv: list[str]) -> int:
    """`sn-agent voice <dev|start|console|download-files>` hands over to
    LiveKit's own CLI. `download-files` fetches the voice detector and turn
    detector models once, before the first run."""

    import sys

    settings = get_settings()
    sys.argv = ["sn-agent voice", *argv]
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            ws_url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret.get_secret_value(),
            # Several models per process; allow time to load them, and keep
            # one process loaded and waiting so the first caller isn't kept waiting.
            initialize_process_timeout=120.0,
            num_idle_processes=1,
            # Windows has no fork; spawn is its only start method.
            multiprocessing_context="spawn" if sys.platform == "win32" else "forkserver",
        )
    )
    return 0
