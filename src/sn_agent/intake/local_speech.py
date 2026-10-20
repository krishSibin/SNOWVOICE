"""Speech-to-text and text-to-speech that run on this machine: no external
API, no per-minute cost, and no audio leaving the network.

- STT: faster-whisper. Whisper transcribes whole utterances, so `LiveWhisperSTT`
  re-reads the phrase about every 0.4 s while the user is still talking and
  sends that as an interim transcript (the words appear on the page as they are
  spoken), then transcribes the whole phrase once at the pause.
- TTS: Kokoro (ONNX). LiveKit splits the streamed reply into sentences and the
  first sentence is spoken while the model is still writing the rest.

Both models load once per worker process, in prewarm.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

import numpy as np
from livekit import rtc
from livekit.agents import APIConnectOptions, stt, tts, utils, vad
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, NOT_GIVEN, NotGivenOr

log = logging.getLogger(__name__)

WHISPER_RATE = 16_000
# Already-downloaded models to fall back on, best first, when the configured one
# isn't on this machine and can't be fetched (an office proxy, no network).
WHISPER_FALLBACKS = ("small.en", "distil-small.en", "base.en", "tiny.en")

# Primes whisper with the words a help desk hears, so "VPN" doesn't come back as
# "VPM" and "Outlook" as "out look". Vocabulary only: it is not an instruction.
IT_VOCABULARY = (
    "IT help desk call. VPN, Outlook, Teams, SharePoint, OneDrive, Wi-Fi, laptop, "
    "password reset, account locked, MFA, ServiceNow, SAP, printer, Citrix, error code."
)
# Whisper's own judgement that a segment was not speech, and how unsure it was
# of the words. Above / below these it is usually noise turned into "Yeah." or
# "Thank you."
NO_SPEECH_PROB = 0.5
MIN_AVG_LOGPROB = -1.0
# What whisper says when it is handed noise or a voice across the room. Kept
# only when it was clearly heard: "yeah" can be a real answer to "shall I raise it?".
PHANTOMS = {
    "thank you", "thanks", "thanks for watching", "you", "bye", "good morning", "good night",
    "okay", "ok", "yeah", "so", "hmm", "maybe a lot better", "i'm sorry", "oh",
}
SURE_LOGPROB = -0.4
SURE_NO_SPEECH = 0.2


def keep_transcript(segments) -> str:
    """The words whisper was confident about, or "" when it was guessing at noise."""

    kept = [s for s in segments if s.no_speech_prob < NO_SPEECH_PROB and s.avg_logprob > MIN_AVG_LOGPROB]
    text = " ".join(s.text.strip() for s in kept).strip()
    # Breath and background noise come back as "..." or "-".
    if not any(c.isalnum() for c in text):
        return ""
    plain = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if plain in PHANTOMS:
        sure = all(s.avg_logprob > SURE_LOGPROB and s.no_speech_prob < SURE_NO_SPEECH for s in kept)
        if not sure:
            return ""
    return text


class WhisperSTT(stt.STT):
    def __init__(self, model: str = "base.en", *, device: str = "auto", language: str = "en"):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        from faster_whisper import WhisperModel

        started = time.time()
        compute = "int8"
        if device in {"auto", "cuda"}:
            try:
                import ctranslate2

                if ctranslate2.get_cuda_device_count() > 0:
                    device, compute = "cuda", "float16"
                else:
                    device = "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"
        self._model, model = _load_whisper(WhisperModel, model, device, compute)
        self._language = language
        # One transcription at a time per process: whisper already uses every core.
        self._lock = asyncio.Lock()
        log.info("whisper %s loaded on %s in %.1fs", model, device, time.time() - started)

    @property
    def model(self) -> str:
        return "faster-whisper"

    @property
    def provider(self) -> str:
        return "local"

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        audio = np.frombuffer(frame.data, dtype=np.int16)
        if frame.num_channels > 1:
            audio = audio.reshape(-1, frame.num_channels).mean(axis=1)
        audio = audio.astype(np.float32) / 32768.0
        if frame.sample_rate != WHISPER_RATE:
            audio = _resample(audio, frame.sample_rate, WHISPER_RATE)
        lang = language if isinstance(language, str) and language else self._language

        def transcribe() -> str:
            segments, _info = self._model.transcribe(
                audio,
                language=lang,
                beam_size=1,
                condition_on_previous_text=False,
                without_timestamps=True,
                vad_filter=False,
                initial_prompt=IT_VOCABULARY,
            )
            # Not answering noise matters: a reply to it talks over the user.
            return keep_transcript(list(segments))

        async with self._lock:
            started = time.time()
            text = await asyncio.to_thread(transcribe)
        log.info("heard %.1fs of speech, transcribed in %.2fs: %r", len(audio) / WHISPER_RATE, time.time() - started, text)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang, text=text)],
        )


# How often the phrase so far is re-read while the user talks, and how much new
# audio there must be to bother. Each pass is a full whisper run on the CPU.
INTERIM_EVERY = 0.4
INTERIM_MIN_NEW = 0.3
INTERIM_MAX_PHRASE = 15.0
PREFIX = 0.5  # audio kept from just before the voice detector fired


def trust_system_certificates() -> None:
    """Make every HTTPS call in this process trust the Windows / macOS certificate
    store, which holds the office proxy's CA. Verification stays on."""

    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:  # pragma: no cover - truststore is a declared dependency
        pass


def _load_whisper(whisper_model, name: str, device: str, compute: str):
    """The configured model if it is downloaded (or can be), else the best one
    that already is. Returns the model and the name actually loaded."""

    try:
        return whisper_model(name, device=device, compute_type=compute, local_files_only=True), name
    except Exception:  # noqa: BLE001 - not in the cache
        pass
    try:
        trust_system_certificates()
        return whisper_model(name, device=device, compute_type=compute), name
    except Exception as exc:  # noqa: BLE001
        for fallback in WHISPER_FALLBACKS:
            if fallback == name:
                continue
            try:
                loaded = whisper_model(fallback, device=device, compute_type=compute, local_files_only=True)
            except Exception:  # noqa: BLE001
                continue
            log.warning(
                "whisper %s is not downloaded and could not be fetched (%s); using %s instead. "
                "Run scripts/download_voice_models.py to get it.",
                name, type(exc).__name__, fallback,
            )
            return loaded, fallback
        raise


class LiveWhisperSTT(stt.STT):
    """WhisperSTT as a streaming recognizer with interim results."""

    def __init__(self, whisper: WhisperSTT, vad_model: vad.VAD, *, language: str = "en"):
        super().__init__(capabilities=stt.STTCapabilities(streaming=True, interim_results=True))
        self._whisper = whisper
        self._vad = vad_model
        self._language = language

    @property
    def model(self) -> str:
        return self._whisper.model

    @property
    def provider(self) -> str:
        return self._whisper.provider

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        return await self._whisper.recognize(buffer=buffer, language=language, conn_options=conn_options)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.RecognizeStream:
        return _LiveWhisperStream(self, language=language if isinstance(language, str) else self._language)


def _seconds(frames) -> float:
    return sum(f.duration for f in frames)


class _LiveWhisperStream(stt.RecognizeStream):
    def __init__(self, live: LiveWhisperSTT, *, language: str):
        # Retries happen inside each whisper call, not by restarting the stream.
        super().__init__(stt=live, conn_options=APIConnectOptions(max_retry=0))
        self._live = live
        self._language = language

    async def _metrics_monitor_task(self, event_aiter) -> None:
        async for _ in event_aiter:  # whisper reports its own metrics
            pass

    async def _transcribe(self, frames) -> str:
        event = await self._live._whisper.recognize(buffer=utils.merge_frames(frames), language=self._language)
        return event.alternatives[0].text if event.alternatives else ""

    def _send(self, kind: stt.SpeechEventType, text: str) -> None:
        self._event_ch.send_nowait(
            stt.SpeechEvent(type=kind, alternatives=[stt.SpeechData(language=self._language, text=text)])
        )

    async def _run(self) -> None:
        vad_stream = self._live._vad.stream()
        recent: deque[rtc.AudioFrame] = deque()
        phrase: list[rtc.AudioFrame] = []
        speaking = False
        generation = 0  # bumps per phrase, so a late interim can't land on the next one

        async def forward_input() -> None:
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    vad_stream.flush()
                    continue
                vad_stream.push_frame(item)
                if speaking:
                    phrase.append(item)
                else:
                    recent.append(item)
                    while recent and _seconds(recent) > PREFIX:
                        recent.popleft()
            vad_stream.end_input()

        async def follow_vad() -> None:
            nonlocal speaking, phrase, generation
            async for event in vad_stream:
                if event.type == vad.VADEventType.START_OF_SPEECH:
                    generation += 1
                    phrase = list(recent)
                    speaking = True
                    self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))
                elif event.type == vad.VADEventType.END_OF_SPEECH:
                    speaking = False
                    generation += 1
                    phrase = []
                    end = time.time() - event.silence_duration - event.inference_duration
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH, speech_end_time=end)
                    )
                    text = await self._transcribe(event.frames)
                    if text:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(
                                type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                                alternatives=[stt.SpeechData(language=self._language, text=text)],
                                speech_end_time=end,
                            )
                        )

        async def interims() -> None:
            done_up_to = 0.0
            while True:
                await asyncio.sleep(INTERIM_EVERY)
                if not speaking:
                    done_up_to = 0.0
                    continue
                heard = _seconds(phrase)
                if heard - done_up_to < INTERIM_MIN_NEW or heard > INTERIM_MAX_PHRASE:
                    continue
                done_up_to, this_phrase = heard, generation
                text = await self._transcribe(list(phrase))
                if text and speaking and generation == this_phrase:
                    self._send(stt.SpeechEventType.INTERIM_TRANSCRIPT, text)

        tasks = [
            asyncio.create_task(forward_input(), name="forward_input"),
            asyncio.create_task(follow_vad(), name="follow_vad"),
            asyncio.create_task(interims(), name="interims"),
        ]
        try:
            await asyncio.gather(*tasks[:2])
        finally:
            await utils.aio.cancel_and_wait(*tasks)
            await vad_stream.aclose()


class KokoroTTS(tts.TTS):
    def __init__(self, model_path: str, voices_path: str, *, voice: str = "af_heart", speed: float = 1.05):
        super().__init__(capabilities=tts.TTSCapabilities(streaming=False), sample_rate=24_000, num_channels=1)
        from kokoro_onnx import Kokoro

        started = time.time()
        self._kokoro = Kokoro(model_path, voices_path)
        self._voice = voice
        self._speed = speed
        self._lock = asyncio.Lock()
        # The first synthesis is slow while the runtime warms up; pay it now.
        self._kokoro.create("Ready.", voice=voice, speed=speed, lang="en-us")
        log.info("kokoro voice %s loaded in %.1fs", voice, time.time() - started)

    @property
    def model(self) -> str:
        return "kokoro"

    @property
    def provider(self) -> str:
        return "local"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _KokoroStream(tts=self, input_text=text, conn_options=conn_options)

    async def render(self, text: str) -> np.ndarray:
        async with self._lock:
            started = time.time()
            samples, _rate = await asyncio.to_thread(
                self._kokoro.create, text, voice=self._voice, speed=self._speed, lang="en-us"
            )
        log.info("spoke %d characters, synthesized in %.2fs", len(text), time.time() - started)
        return samples


class _KokoroStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=self._tts.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        text = self._input_text.strip()
        if text:
            samples = await self._tts.render(text)
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
            output_emitter.push(pcm.tobytes())
        output_emitter.flush()


def _resample(audio: np.ndarray, source: int, target: int) -> np.ndarray:
    if len(audio) == 0:
        return audio
    duration = len(audio) / source
    points = np.linspace(0, len(audio) - 1, int(duration * target))
    return np.interp(points, np.arange(len(audio)), audio).astype(np.float32)
