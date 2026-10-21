import { useEffect, useRef } from "react";
import type { AgentState } from "./useVoiceChat";
import { useAudioLevel } from "./useAudioLevel";

/**
 * The voice-mode orb: a soft, icy blob drawn on a canvas. It breathes when
 * idle, swells with the caller's voice while listening, swirls while the
 * assistant thinks, and ripples with the assistant's voice while it speaks.
 */
export function VoiceOrb({
  state,
  agentTrack,
  micTrack,
  size = 260,
}: {
  state: AgentState;
  agentTrack: MediaStreamTrack | null;
  micTrack: MediaStreamTrack | null;
  size?: number;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const agentLevel = useAudioLevel(agentTrack);
  const micLevel = useAudioLevel(micTrack);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    const el = canvas.current;
    const ctx = el?.getContext("2d");
    if (!el || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    el.width = size * dpr;
    el.height = size * dpr;
    ctx.scale(dpr, dpr);

    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const c = size / 2;
    let amp = 0;
    let spin = 0;
    let frame = 0;
    const start = performance.now();

    const blob = (radius: number, wobble: number, t: number, phase: number) => {
      ctx.beginPath();
      const n = 120;
      for (let i = 0; i <= n; i++) {
        const a = (i / n) * Math.PI * 2;
        const r =
          radius *
          (1 +
            wobble *
              (0.55 * Math.sin(3 * a + t * 1.3 + phase) +
                0.3 * Math.sin(5 * a - t * 1.7 + phase * 2) +
                0.15 * Math.sin(7 * a + t * 2.3 - phase)));
        const x = c + r * Math.cos(a + spin);
        const y = c + r * Math.sin(a + spin);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.closePath();
    };

    const draw = (now: number) => {
      const t = still ? 0 : (now - start) / 1000;
      const s = stateRef.current;
      const live = s === "speaking" ? agentLevel.current : s === "listening" ? micLevel.current : 0;
      amp += (live - amp) * 0.25;
      spin += s === "thinking" && !still ? 0.035 : 0.002;
      const breathe = still ? 0 : Math.sin(t * 1.6) * 0.015;
      const base = size * 0.3 * (1 + breathe + amp * 0.22);
      const wobble = (s === "thinking" ? 0.06 : 0.025) + amp * 0.12;

      ctx.clearRect(0, 0, size, size);

      // Halo
      const halo = ctx.createRadialGradient(c, c, base * 0.6, c, c, size / 2);
      halo.addColorStop(0, `rgba(120, 190, 255, ${0.28 + amp * 0.35})`);
      halo.addColorStop(1, "rgba(120, 190, 255, 0)");
      ctx.fillStyle = halo;
      ctx.fillRect(0, 0, size, size);

      // Three translucent layers, each moving slightly out of step.
      const layers: [number, string, string, number][] = [
        [1.08, "rgba(79, 163, 236, 0.35)", "rgba(13, 43, 82, 0.15)", 0],
        [1.0, "rgba(140, 205, 255, 0.55)", "rgba(47, 127, 209, 0.45)", 2.1],
        [0.9, "rgba(255, 255, 255, 0.95)", "rgba(79, 163, 236, 0.85)", 4.2],
      ];
      for (const [scale, inner, outer, phase] of layers) {
        const g = ctx.createRadialGradient(c - base * 0.3, c - base * 0.35, base * 0.1, c, c, base * scale * 1.15);
        g.addColorStop(0, inner);
        g.addColorStop(1, outer);
        ctx.fillStyle = g;
        blob(base * scale, wobble, t, phase);
        ctx.fill();
      }

      // While thinking, a ring of snow orbits the orb.
      if (s === "thinking") {
        for (let i = 0; i < 8; i++) {
          const a = spin * 2 + (i / 8) * Math.PI * 2;
          const r = base * 1.35;
          ctx.beginPath();
          ctx.fillStyle = `rgba(220, 240, 255, ${0.35 + 0.5 * ((i + 1) / 8)})`;
          ctx.arc(c + r * Math.cos(a), c + r * Math.sin(a), 2 + (i % 3), 0, Math.PI * 2);
          ctx.fill();
        }
      }

      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, [size, agentLevel, micLevel]);

  return <canvas ref={canvas} className="orb" style={{ width: size, height: size }} role="img" aria-label={`Assistant ${state || "idle"}`} />;
}
