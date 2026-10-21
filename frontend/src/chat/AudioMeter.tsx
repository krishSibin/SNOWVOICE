import { useEffect, useRef } from "react";

const BARS = 5;

/**
 * A live level meter for one audio track: the user's microphone, so they can
 * see they are being picked up, or the assistant's voice. Bars are moved
 * directly on each animation frame, without re-rendering React.
 */
export function AudioMeter({
  track,
  mine = false,
  label,
}: {
  track: MediaStreamTrack | null;
  mine?: boolean;
  label: string;
}) {
  const bars = useRef<(HTMLSpanElement | null)[]>([]);

  useEffect(() => {
    if (!track) return;
    const ctx = new AudioContext();
    void ctx.resume();
    const source = ctx.createMediaStreamSource(new MediaStream([track]));
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.7;
    source.connect(analyser);
    const data = new Uint8Array(analyser.frequencyBinCount);
    // Speech lives roughly between 100 Hz and 4 kHz.
    const hz = ctx.sampleRate / analyser.fftSize;
    const lo = Math.max(1, Math.floor(100 / hz));
    const hi = Math.min(data.length, Math.ceil(4000 / hz));
    const per = Math.max(1, Math.floor((hi - lo) / BARS));
    let frame = 0;

    const tick = () => {
      analyser.getByteFrequencyData(data);
      for (let b = 0; b < BARS; b++) {
        let sum = 0;
        for (let i = lo + b * per; i < lo + (b + 1) * per; i++) sum += data[i];
        const level = sum / per / 255;
        const el = bars.current[b];
        if (el) el.style.transform = `scaleY(${Math.max(0.12, Math.min(1, level * 1.8))})`;
      }
      frame = requestAnimationFrame(tick);
    };
    tick();
    const els = bars.current;
    return () => {
      cancelAnimationFrame(frame);
      source.disconnect();
      void ctx.close();
      els.forEach((el) => {
        if (el) el.style.transform = "";
      });
    };
  }, [track]);

  return (
    <span className={`meter${mine ? " mine" : ""}${track ? "" : " idle"}`} role="img" aria-label={label}>
      {Array.from({ length: BARS }, (_, i) => (
        <span
          key={i}
          ref={(el) => {
            bars.current[i] = el;
          }}
        />
      ))}
    </span>
  );
}
