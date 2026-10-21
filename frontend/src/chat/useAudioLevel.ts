import { useEffect, useRef } from "react";

/**
 * The loudness (0..1) of one audio track, kept in a ref and updated on every
 * animation frame, so a canvas can read it without re-rendering React.
 */
export function useAudioLevel(track: MediaStreamTrack | null) {
  const level = useRef(0);

  useEffect(() => {
    level.current = 0;
    if (!track) return;
    const ctx = new AudioContext();
    void ctx.resume();
    const source = ctx.createMediaStreamSource(new MediaStream([track]));
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    analyser.smoothingTimeConstant = 0.6;
    source.connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    let frame = 0;

    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      // RMS of speech sits around 0.02..0.25; stretch it to fill 0..1.
      const target = Math.min(1, Math.sqrt(sum / data.length) * 5);
      // Rise fast, fall slowly, so the orb doesn't flicker between syllables.
      level.current += (target - level.current) * (target > level.current ? 0.5 : 0.08);
      frame = requestAnimationFrame(tick);
    };
    tick();
    return () => {
      cancelAnimationFrame(frame);
      source.disconnect();
      void ctx.close();
      level.current = 0;
    };
  }, [track]);

  return level;
}
