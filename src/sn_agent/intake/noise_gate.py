"""A noise gate on the caller's microphone, applied before the voice detector
and whisper hear anything.

Someone talking into their own microphone is much louder than voices across
the room, a fan or typing. The gate passes audio only while it is clearly
loud enough, and replaces the rest with silence. So distant voices no longer
start a turn, interrupt the assistant, or turn up in the transcript as words
the caller never said.

"Loud enough" is the highest of three levels:
- a fixed minimum (`min_db`);
- the room's background plus a margin; the background is learned as the call
  goes on, dropping quickly to a quiet room and rising only slowly;
- the caller's typical speaking level minus `relative_db`. The caller is the loudest thing
  the microphone hears, so once they have spoken, chatter far below their
  level is muted even in a loud office.

Opening is instant; closing waits a moment, so the ends of words and short
pauses between them are kept. Each phrase let through, and the loudest
background muted, are logged so the levels can be tuned from real numbers.
"""

from __future__ import annotations

import logging
import math

import numpy as np
from livekit import rtc

log = logging.getLogger(__name__)

SILENCE_DB = -100.0
VOICE_SMOOTHING = 0.05  # per 50 ms frame: about a second of speech to settle
REPORT_EVERY_S = 5.0  # at most one "muted background" line per this many seconds


def level_db(samples: np.ndarray) -> float:
    """RMS level of int16 samples, in dB below full scale."""

    if samples.size == 0:
        return SILENCE_DB
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) / 32768.0
    return 20 * math.log10(rms) if rms > 0 else SILENCE_DB


class NoiseGate(rtc.FrameProcessor[rtc.AudioFrame]):
    def __init__(
        self,
        *,
        min_db: float = -45.0,
        margin_db: float = 12.0,
        relative_db: float = 15.0,
        hold_s: float = 0.35,
        floor_rise_db_per_s: float = 1.0,
        voice_decay_db_per_s: float = 0.5,
    ):
        """
        Args:
            min_db: Audio quieter than this never opens the gate.
            margin_db: How far above the room's background speech must be.
            relative_db: How far below the caller's own voice audio is muted.
            hold_s: How long the gate stays open after the voice drops.
            floor_rise_db_per_s: How fast the background estimate may rise.
            voice_decay_db_per_s: How fast the caller's level is forgotten,
                in case they move away from the microphone.
        """
        self._enabled = True
        self.min_db = min_db
        self.margin_db = margin_db
        self.relative_db = relative_db
        self.hold_s = hold_s
        self.floor_rise = floor_rise_db_per_s
        self.voice_decay = voice_decay_db_per_s
        self.floor_db = min_db - margin_db  # the room's background, learned
        self.voice_db = SILENCE_DB  # the caller's own level, learned
        self._open_for = 0.0  # seconds the gate stays open without more loud audio
        self._phrase_s = 0.0
        self._phrase_peak = SILENCE_DB
        self._muted_peak = SILENCE_DB
        self._since_report = 0.0
        self.passed_s = 0.0
        self.blocked_s = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def threshold_db(self) -> float:
        return max(self.min_db, self.floor_db + self.margin_db, self.voice_db - self.relative_db)

    def _process(self, frame: rtc.AudioFrame) -> rtc.AudioFrame:
        if not self._enabled:
            return frame
        samples = np.frombuffer(frame.data, dtype=np.int16)
        seconds = frame.samples_per_channel / frame.sample_rate if frame.sample_rate else 0.0
        db = level_db(samples)
        loud = db >= self.threshold_db

        # Learn the background: follow a quieter room at once, a louder one slowly.
        if db < self.floor_db:
            if db > SILENCE_DB:
                self.floor_db = db
        elif not loud:
            self.floor_db = min(db, self.floor_db + self.floor_rise * seconds)
        # Learn the caller's typical level from what gets through, as a running
        # average so one knock on the desk can't raise the bar; forget it slowly.
        if loud:
            fresh = self.voice_db <= SILENCE_DB
            self.voice_db = db if fresh else self.voice_db + VOICE_SMOOTHING * (db - self.voice_db)
        elif self.voice_db > SILENCE_DB and db < self.floor_db + self.margin_db:
            # Only while the room is quiet: forgetting during chatter would
            # lower the bar until the chatter itself got through.
            self.voice_db -= self.voice_decay * seconds

        if loud:
            self._open_for = self.hold_s
        elif self._open_for > 0:
            self._open_for -= seconds
        else:
            self._end_phrase()
            self.blocked_s += seconds
            self._note_muted(db, seconds)
            return rtc.AudioFrame(
                data=bytes(samples.nbytes),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                samples_per_channel=frame.samples_per_channel,
            )
        self.passed_s += seconds
        self._phrase_s += seconds
        self._phrase_peak = max(self._phrase_peak, db)
        return frame

    def _end_phrase(self) -> None:
        if self._phrase_s <= 0:
            return
        log.info(
            "noise gate: let through %.1fs, loudest %.0f dB (gate at %.0f dB, room %.0f dB)",
            self._phrase_s, self._phrase_peak, self.threshold_db, self.floor_db,
        )
        self._phrase_s, self._phrase_peak = 0.0, SILENCE_DB

    def _note_muted(self, db: float, seconds: float) -> None:
        # Only sounds that stood out from the room: those are the ones to tune against.
        if db >= self.floor_db + self.margin_db:
            self._muted_peak = max(self._muted_peak, db)
        self._since_report += seconds
        if self._since_report >= REPORT_EVERY_S and self._muted_peak > SILENCE_DB:
            log.info(
                "noise gate: muted background up to %.0f dB (gate at %.0f dB)", self._muted_peak, self.threshold_db
            )
            self._muted_peak, self._since_report = SILENCE_DB, 0.0

    def _close(self) -> None:
        self._end_phrase()
        log.info(
            "noise gate: passed %.0fs, muted %.0fs; room %.0f dB, caller %.0f dB, gate at %.0f dB",
            self.passed_s, self.blocked_s, self.floor_db, self.voice_db, self.threshold_db,
        )
