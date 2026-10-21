"""Live transcripts: the words appear while the user is still talking, and the
whole phrase is transcribed once at the pause."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

pytest.importorskip("livekit.agents")

from livekit import rtc  # noqa: E402
from livekit.agents import stt, utils, vad  # noqa: E402

from sn_agent.intake.local_speech import (  # noqa: E402
    ClauseTokenizer,
    GatewaySTT,
    LiveWhisperSTT,
    _load_whisper,
    clean_gateway_transcript,
    collapse_repeats,
    keep_transcript,
    split_clauses,
)

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
    def __init__(self, text, avg_logprob=-0.2, no_speech_prob=0.05, compression_ratio=1.2):
        self.text, self.avg_logprob, self.no_speech_prob = text, avg_logprob, no_speech_prob
        self.compression_ratio = compression_ratio


def test_phrases_whisper_invents_from_noise_are_dropped_unless_clearly_heard():
    assert keep_transcript([Seg(" Good morning.", avg_logprob=-0.7)]) == ""
    assert keep_transcript([Seg(" Thank you.", no_speech_prob=0.35)]) == ""
    assert keep_transcript([Seg(" ...")]) == ""
    assert keep_transcript([Seg(" Yeah.")]) == "Yeah."  # a clear yes to "shall I raise it?"
    assert keep_transcript([Seg(" My VPN says error 809.", avg_logprob=-0.7)]) == "My VPN says error 809."
    assert keep_transcript([Seg(" mumble", avg_logprob=-1.3)]) == ""


VOICE, CHATTER = 1000, 400  # sample values: the caller, and the room


def sound_frame(level: int) -> rtc.AudioFrame:
    return rtc.AudioFrame(np.full(FRAME, level, dtype=np.int16).tobytes(), RATE, 1, FRAME)


class ScriptedWhisper(stt.STT):
    """Hears the caller's words only in audio that holds the caller's voice."""

    def __init__(self):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.text = ""

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        audio = np.frombuffer(utils.merge_frames(buffer).data, dtype=np.int16)
        text = self.text if (audio == VOICE).any() else ""
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language="en", text=text)],
        )


def test_the_turn_ends_when_the_words_stop_though_the_room_stays_loud():
    async def run():
        whisper = ScriptedWhisper()
        stream = LiveWhisperSTT(whisper, LoudnessVAD()).stream()
        events: list[tuple[float, stt.SpeechEvent]] = []
        started = asyncio.get_running_loop().time()

        async def read():
            async for event in stream:
                events.append((asyncio.get_running_loop().time() - started, event))

        reader = asyncio.create_task(read())

        async def sound(seconds: float, level: int):
            for _ in range(int(seconds / 0.02)):
                stream.push_frame(sound_frame(level))
                await asyncio.sleep(0.02)

        whisper.text = "My VPN keeps"
        await sound(0.8, VOICE)
        whisper.text = "My VPN keeps dropping."
        await sound(0.8, VOICE)
        # The caller has finished; the room stays loud, with no words of its own.
        await sound(4.0, CHATTER)
        sound_stopped = asyncio.get_running_loop().time() - started
        await sound(0.2, 0)
        await asyncio.sleep(0.3)
        stream.end_input()
        await reader
        return events, sound_stopped

    events, sound_stopped = asyncio.run(run())
    finals = [(t, e) for t, e in events if e.type == stt.SpeechEventType.FINAL_TRANSCRIPT]
    ends = [t for t, e in events if e.type == stt.SpeechEventType.END_OF_SPEECH]

    assert [e.alternatives[0].text for _, e in finals] == ["My VPN keeps dropping."]  # one turn, no chatter turn
    assert ends and ends[0] < sound_stopped - 1.0  # ended while the room was still loud


def test_whisper_loops_and_stutters_are_cleaned_up():
    looping = Seg(" E-N-S-S-S-S-S-S-S-S-S-S-S-S-S-S-S-S-S-S", compression_ratio=6.0)
    assert keep_transcript([Seg(" My VPN keeps dropping."), looping]) == "My VPN keeps dropping."
    assert (
        collapse_repeats("on the VPN, it's showing some, it's showing some, it's showing some, error 809")
        == "on the VPN, it's showing some, error 809"
    )
    assert collapse_repeats("Can you help me? Can you help me?") == "Can you help me?"
    assert collapse_repeats("no no it is not working") == "no no it is not working"  # short repeats are speech


def test_a_long_sentence_is_spoken_clause_by_clause():
    step = "Open Settings on your laptop, then go to Network and Internet, and switch the VPN off and on again."
    assert split_clauses(step) == [
        "Open Settings on your laptop,",
        "then go to Network and Internet,",
        "and switch the VPN off and on again.",
    ]
    # Short sentences, numbers and short last clauses stay whole.
    assert split_clauses("Yes, I can hear you.") == ["Yes, I can hear you."]
    assert split_clauses("It affects 1,000 users across the whole office building.") == [
        "It affects 1,000 users across the whole office building."
    ]
    assert split_clauses("Restart the Outlook app on your laptop, please.") == ["Restart the Outlook app on your laptop, please."]


def test_the_first_clause_is_ready_before_the_reply_is_finished():
    reply = "Open Settings on your laptop, then go to Network and Internet, and switch the VPN off and on again."

    async def run() -> tuple[str, list[str]]:
        stream = ClauseTokenizer().stream()
        written = reply.index("switch")  # the model is still writing the first sentence
        for i in range(0, written, 7):  # streamed in small chunks, like the model
            stream.push_text(reply[i : min(i + 7, written)])
        first = (await asyncio.wait_for(stream.__anext__(), 1.0)).token
        stream.push_text(reply[written:])
        stream.end_input()
        return first, [first] + [t.token async for t in stream]

    first, pieces = asyncio.run(run())
    assert first == "Open Settings on your laptop,"
    assert " ".join(pieces) == reply


class FixedSTT(stt.STT):
    def __init__(self, text: str):
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.text = text

    async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
        return stt.SpeechEvent(type=stt.SpeechEventType.FINAL_TRANSCRIPT, alternatives=[stt.SpeechData(language="en", text=self.text)])


def test_noise_from_the_gateway_never_becomes_a_turn():
    for noise in [".", " - ", "You", "Thanks for watching!", "IT help desk call. VPN, Outlook, Teams."]:
        assert clean_gateway_transcript(noise) == "", noise
    # Short real answers are kept: the gateway gives no confidence to judge them by.
    for answer in ["Yeah.", "Thank you.", "No.", "Yes, raise it."]:
        assert clean_gateway_transcript(answer) == answer
    assert clean_gateway_transcript("Can you help me? Can you help me?") == "Can you help me?"

    async def hear(text: str) -> str:
        audio = [frame(True) for _ in range(5)]
        event = await GatewaySTT(FixedSTT(text)).recognize(buffer=audio)
        return event.alternatives[0].text

    assert asyncio.run(hear(".")) == ""
    assert asyncio.run(hear("My VPN says error 809.")) == "My VPN says error 809."
