"""Live transcripts: the words appear while the user is still talking, and the
whole phrase is transcribed once at the pause."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

pytest.importorskip("livekit.agents")

from livekit import rtc  # noqa: E402
from livekit.agents import stt, utils, vad  # noqa: E402

from sn_agent.intake.local_speech import LiveWhisperSTT, _load_whisper, keep_transcript  # noqa: E402

RATE = 16_000
FRAME = 320  # 20 ms


def frame(loud: bool) -> rtc.AudioFrame:
    data = np.full(FRAME, 1000 if loud else 0, dtype=np.int16)
    return rtc.AudioFrame(data.tobytes(), RATE, 1, FRAME)


class LoudnessVAD(vad.VAD):
    """Speech is any non-silent frame; three silent frames end it."""

    def __init__(self):
        super().__init__(capabilities=vad.VADCapabilities(update_interval=0.02))

    def stream(self) -> vad.VADStream:
        return _LoudnessStream(self)


class _LoudnessStream(vad.VADStream):
    async def _main_task(self) -> None:
        speech: list[rtc.AudioFrame] = []
        quiet = 0
        async for item in self._input_ch:
            if isinstance(item, self._FlushSentinel):
                continue
            loud = bool(np.frombuffer(item.data, dtype=np.int16).any())
            if loud and not speech:
                self._event_ch.send_nowait(vad.VADEvent(vad.VADEventType.START_OF_SPEECH, 0, 0, 0, 0))
            if loud or speech:
                speech.append(item)
            quiet = 0 if loud else quiet + 1
            if speech and quiet == 3:
                self._event_ch.send_nowait(vad.VADEvent(vad.VADEventType.END_OF_SPEECH, 0, 0, 0, 0.06, frames=speech))
                speech = []


class SecondsWhisper(stt.STT):
    """Answers with how much audio it was given, so the test can see which call made which text."""

    def __init__(self):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.calls = 0

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        self.calls += 1
        seconds = utils.merge_frames(buffer).duration
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=f"{seconds:.1f}s")],
        )


def test_words_arrive_while_talking_and_the_phrase_is_final_at_the_pause():
    async def run():
        whisper = SecondsWhisper()
        stream = LiveWhisperSTT(whisper, LoudnessVAD()).stream()
        events: list[stt.SpeechEvent] = []

        async def read():
            async for event in stream:
                events.append(event)

        reader = asyncio.create_task(read())
        for loud in [False] * 10 + [True] * 100 + [False] * 10:  # 2 s of talking
            stream.push_frame(frame(loud))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        stream.end_input()
        await reader
        return events

    events = asyncio.run(run())
    kinds = [e.type for e in events]

    assert kinds[0] == stt.SpeechEventType.START_OF_SPEECH
    interims = [e for e in events if e.type == stt.SpeechEventType.INTERIM_TRANSCRIPT]
    assert len(interims) >= 1  # text showed up before the user stopped
    assert kinds.index(stt.SpeechEventType.INTERIM_TRANSCRIPT) < kinds.index(stt.SpeechEventType.END_OF_SPEECH)
    final = [e for e in events if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
    assert len(final) == 1 and final[0].alternatives[0].text == "2.1s"  # the whole phrase, plus the trailing silence
    assert kinds.index(stt.SpeechEventType.FINAL_TRANSCRIPT) > kinds.index(stt.SpeechEventType.END_OF_SPEECH)


def test_a_model_that_cannot_be_downloaded_falls_back_to_one_that_is(caplog):
    tried = []

    class Whisper:
        def __init__(self, name, device, compute_type, local_files_only=False):
            tried.append((name, local_files_only))
            if not (name == "base.en" and local_files_only):
                raise OSError("CERTIFICATE_VERIFY_FAILED" if not local_files_only else "not cached")

    _model, loaded = _load_whisper(Whisper, "distil-small.en", "cpu", "int8")

    assert loaded == "base.en"
    assert ("distil-small.en", False) in tried  # it did try to fetch the configured one
    assert "Run scripts/download_voice_models.py" in caplog.text


class Seg:
    def __init__(self, text, avg_logprob=-0.2, no_speech_prob=0.05):
        self.text, self.avg_logprob, self.no_speech_prob = text, avg_logprob, no_speech_prob


def test_phrases_whisper_invents_from_noise_are_dropped_unless_clearly_heard():
    assert keep_transcript([Seg(" Good morning.", avg_logprob=-0.7)]) == ""
    assert keep_transcript([Seg(" Thank you.", no_speech_prob=0.35)]) == ""
    assert keep_transcript([Seg(" ...")]) == ""
    assert keep_transcript([Seg(" Yeah.")]) == "Yeah."  # a clear yes to "shall I raise it?"
    assert keep_transcript([Seg(" My VPN says error 809.", avg_logprob=-0.7)]) == "My VPN says error 809."
    assert keep_transcript([Seg(" mumble", avg_logprob=-1.3)]) == ""
