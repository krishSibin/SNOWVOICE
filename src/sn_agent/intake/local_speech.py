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
from livekit.agents import APIConnectOptions, stt, tokenize, tts, utils, vad
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
# Whisper's own sign of a loop ("S-S-S-S..." or one phrase over and over):
# text that compresses this well is not what anyone said.
MAX_COMPRESSION_RATIO = 2.4


def collapse_repeats(text: str, min_words: int = 3) -> str:
    """Drops a run of words repeated straight after itself, which whisper
    produces on noisy audio: "it's showing some, it's showing some" keeps one."""

    words = text.split()
    plain = [re.sub(r"[^\w']", "", w.lower()) for w in words]
    n = max(min_words, 1)
    out: list[str] = []
    out_plain: list[str] = []
    i = 0
    while i < len(words):
        repeated = False
        for size in range(min(12, len(out_plain)), n - 1, -1):
            if plain[i : i + size] == out_plain[-size:] and all(plain[i : i + size]):
                i += size
                repeated = True
                break
        if not repeated:
            out.append(words[i])
            out_plain.append(plain[i])
            i += 1
    return " ".join(out)


def keep_transcript(segments) -> str:
    """The words whisper was confident about, or "" when it was guessing at noise."""

    kept = [
        s
        for s in segments
        if s.no_speech_prob < NO_SPEECH_PROB
        and s.avg_logprob > MIN_AVG_LOGPROB
        and getattr(s, "compression_ratio", 0.0) <= MAX_COMPRESSION_RATIO
    ]
    text = collapse_repeats(" ".join(s.text.strip() for s in kept).strip())
    # Breath and background noise come back as "..." or "-".
    if not any(c.isalnum() for c in text):
        return ""
    plain = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if plain in PHANTOMS:
        sure = all(s.avg_logprob > SURE_LOGPROB and s.no_speech_prob < SURE_NO_SPEECH for s in kept)
        if not sure:
            return ""
    return text


# What the gateway's whisper returns for noise. It gives no confidences, so
# only phrases nobody says to a help desk are dropped; "yeah" and "thank you"
# can be real answers and are kept.
GATEWAY_PHANTOMS = {
    "you", "thanks for watching", "thank you for watching", "please subscribe",
    "subtitles by the amara org community",
}


def clean_gateway_transcript(text: str) -> str:
    """The gateway's transcript, or "" when it was only noise."""

    text = collapse_repeats((text or "").strip())
    # Breath and background noise come back as "." or "-".
    if not any(c.isalnum() for c in text):
        return ""
    plain = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if plain in GATEWAY_PHANTOMS:
        return ""
    # On silence whisper can read its vocabulary prompt back.
    if plain.startswith("it help desk call"):
        return ""
    return text


class GatewaySTT(stt.STT):
    """The gateway's whisper, with what it hears in noise dropped before it
    can start a turn."""

    def __init__(self, inner: stt.STT):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._inner = inner

    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return self._inner.provider

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> stt.SpeechEvent:
        started = time.time()
        # Retries happen in this wrapper's recognize, not twice over.
        event = await self._inner.recognize(
            buffer=buffer, language=language, conn_options=APIConnectOptions(max_retry=0, timeout=conn_options.timeout)
        )
        for alt in event.alternatives:
            heard, alt.text = alt.text, clean_gateway_transcript(alt.text)
            if heard and not alt.text:
                log.info("dropped %r from the gateway as noise", heard)
        log.info("gateway transcribed in %.2fs: %r", time.time() - started, event.alternatives[0].text if event.alternatives else "")
        return event


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
# In a noisy room the sound never stops, so the voice detector never ends the
# turn. When the words have stopped changing for this long (and this many
# re-reads in a row), the turn ends on what was said and the rest is treated as
# the room. Longer than the 0.45 s pause that ends a turn in a quiet room, so a
# speaker who pauses to think is not cut off.
STALL_SECONDS = 1.2
STALL_PASSES = 2


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
        speaking = False  # the voice detector hears sound
        generation = 0  # bumps per phrase, so a late interim can't land on the next one
        # Ending a turn when the words stop, though the sound goes on (a noisy
        # room): what has been said, when it last changed, how many re-reads
        # since, and whether this stretch of sound was already cut into a turn.
        said = ""
        said_at = 0.0
        unchanged = 0
        in_turn = False  # START_OF_SPEECH sent and no END_OF_SPEECH yet
        cut = False

        def start_turn() -> None:
            nonlocal in_turn
            if not in_turn:
                in_turn = True
                self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH))

        def end_turn(text: str, end: float) -> None:
            nonlocal in_turn
            in_turn = False
            self._event_ch.send_nowait(stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH, speech_end_time=end))
            if text:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        alternatives=[stt.SpeechData(language=self._language, text=text)],
                        speech_end_time=end,
                    )
                )

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
            nonlocal speaking, phrase, generation, said, said_at, unchanged, cut
            async for event in vad_stream:
                if event.type == vad.VADEventType.START_OF_SPEECH:
                    generation += 1
                    phrase = list(recent)
                    speaking, cut = True, False
                    said, said_at, unchanged = "", time.time(), 0
                    start_turn()
                elif event.type == vad.VADEventType.END_OF_SPEECH:
                    speaking = False
                    generation += 1
                    # After a cut, only the sound since then is still unheard.
                    frames = phrase if cut else event.frames
                    phrase = []
                    end = time.time() - event.silence_duration - event.inference_duration
                    if cut and not in_turn and not frames:
                        continue
                    text = await self._transcribe(frames)
                    if cut and not in_turn:
                        if not text:
                            continue  # only background after the cut
                        start_turn()
                    end_turn(text, end)

        async def interims() -> None:
            nonlocal phrase, generation, said, said_at, unchanged, cut
            done_up_to = 0.0
            while True:
                await asyncio.sleep(INTERIM_EVERY)
                if not speaking:
                    done_up_to = 0.0
                    continue
                heard = _seconds(phrase)
                if heard - done_up_to >= INTERIM_MIN_NEW and heard <= INTERIM_MAX_PHRASE:
                    done_up_to, this_phrase = heard, generation
                    text = await self._transcribe(list(phrase))
                    if not (speaking and generation == this_phrase):
                        continue
                    if text and text != said:
                        said, said_at, unchanged = text, time.time(), 0
                        start_turn()
                        self._send(stt.SpeechEventType.INTERIM_TRANSCRIPT, text)
                        continue
                    unchanged += 1
                elif heard > INTERIM_MAX_PHRASE:
                    unchanged += 1  # too long to re-read: count it as nothing new
                # Words, then nothing new for a while, yet still sound: the
                # rest is the room. End the turn on what was said.
                if (
                    said
                    and in_turn
                    and unchanged >= STALL_PASSES
                    and time.time() - said_at >= STALL_SECONDS
                ):
                    log.info("words stopped but the sound went on; ending the turn on %r", said)
                    end_turn(said, said_at)
                    generation += 1
                    phrase = []
                    done_up_to = 0.0
                    said, unchanged, cut = "", 0, True

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


# Kokoro renders a whole piece before any of it plays, at roughly 0.4 s per
# second of speech on a laptop CPU, so a long first sentence meant 2 s or more
# of silence. A long sentence is cut at a comma or semicolon into pieces of at
# least this many characters; the first plays while the next is rendered.
MIN_CLAUSE = 25
CLAUSE_BREAK = re.compile(r"[,;:](?=\s)")


def split_clauses(sentence: str, min_len: int = MIN_CLAUSE) -> list[str]:
    """A sentence cut at commas into pieces no shorter than `min_len`."""

    pieces: list[str] = []
    start = 0
    for match in CLAUSE_BREAK.finditer(sentence):
        cut = match.end()
        if cut - start >= min_len and len(sentence) - cut >= min_len:
            pieces.append(sentence[start:cut].strip())
            start = cut
    pieces.append(sentence[start:].strip())
    return [p for p in pieces if p]


class ClauseTokenizer(tokenize.SentenceTokenizer):
    """Sentences, as LiveKit splits them, with long ones cut into clauses."""

    def __init__(self, min_len: int = MIN_CLAUSE):
        self._sentences = tokenize.blingfire.SentenceTokenizer(retain_format=True)
        self._min_len = min_len

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        return [p for s in self._sentences.tokenize(text) for p in split_clauses(s.strip(), self._min_len)]

    def stream(self, *, language: str | None = None) -> tokenize.SentenceStream:
        # Each piece is an exact slice of the text, so the stream can find it.
        return tokenize.BufferedSentenceStream(tokenizer=self.tokenize, min_token_len=self._min_len, min_ctx_len=10)


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
