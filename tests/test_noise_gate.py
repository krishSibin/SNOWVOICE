"""The microphone noise gate: the caller's voice passes, background is muted."""

import numpy as np
import pytest

rtc = pytest.importorskip("livekit.rtc")

from sn_agent.intake.noise_gate import NoiseGate, level_db  # noqa: E402

RATE = 24_000
FRAME = RATE // 20  # 50 ms


def tone(db: float, seed: int = 0) -> "rtc.AudioFrame":
    """50 ms of noise at roughly the given level."""
    rng = np.random.default_rng(seed)
    amplitude = 32768 * 10 ** (db / 20)
    samples = np.clip(rng.normal(0, amplitude, FRAME), -32768, 32767).astype(np.int16)
    return rtc.AudioFrame(data=samples.tobytes(), sample_rate=RATE, num_channels=1, samples_per_channel=FRAME)


def is_silent(frame) -> bool:
    return not np.frombuffer(frame.data, dtype=np.int16).any()


def run(gate, db: float, frames: int):
    return [gate._process(tone(db, seed=i)) for i in range(frames)]


def test_level_db_matches_the_signal():
    assert abs(level_db(np.frombuffer(tone(-30).data, dtype=np.int16)) - -30) < 1.5
    assert level_db(np.zeros(100, dtype=np.int16)) == -100.0


def test_quiet_background_is_muted_and_close_speech_passes():
    gate = NoiseGate()
    assert all(is_silent(f) for f in run(gate, -55, 40))  # a quiet office, 2 s
    assert not any(is_silent(f) for f in run(gate, -25, 10))  # the caller talking


def test_voices_across_the_room_stay_muted_once_the_room_is_learned():
    gate = NoiseGate()
    run(gate, -62, 40)  # a quiet room
    # Distant chatter: louder than the room, well below the caller's own voice.
    assert all(is_silent(f) for f in run(gate, -48, 20))


def test_short_pauses_between_words_are_kept():
    gate = NoiseGate(hold_s=0.35)
    run(gate, -60, 20)
    run(gate, -25, 5)
    pause = run(gate, -60, 4)  # 200 ms gap, inside the hold
    assert not any(is_silent(f) for f in pause)
    assert all(is_silent(f) for f in run(gate, -60, 10)[-3:])  # then it closes


def test_a_long_sentence_is_not_learned_as_background():
    gate = NoiseGate()
    run(gate, -60, 20)
    assert not any(is_silent(f) for f in run(gate, -28, 200))  # 10 s of talking


def test_disabled_gate_passes_everything():
    gate = NoiseGate()
    gate.enabled = False
    assert not any(is_silent(f) for f in run(gate, -60, 10))


def test_loud_office_chatter_is_muted_once_the_caller_has_spoken():
    gate = NoiseGate(min_db=-45)
    run(gate, -60, 20)
    run(gate, -42, 20)  # chatter louder than the fixed minimum gets through at first
    assert not any(is_silent(f) for f in run(gate, -24, 40))  # the caller talks
    assert all(is_silent(f) for f in run(gate, -42, 40)[10:])  # now the same chatter is muted


def test_one_loud_knock_does_not_mute_the_caller():
    gate = NoiseGate()
    run(gate, -60, 20)
    run(gate, -26, 40)
    run(gate, -5, 1)  # a knock on the desk
    run(gate, -60, 10)
    assert not any(is_silent(f) for f in run(gate, -28, 10))
