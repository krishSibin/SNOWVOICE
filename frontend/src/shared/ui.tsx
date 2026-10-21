import type { ReactNode } from "react";

export type Tone = "good" | "warn" | "bad" | "";

export function Pill({ tone = "", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`pill ${tone}`}>{children}</span>;
}

export function TopBar({ brand, who, children }: { brand: string; who: string; children?: ReactNode }) {
  return (
    <header className="topbar">
      <span className="brand">{brand}</span>
      <span className="spacer" />
      {children}
      <span className="who">{who}</span>
      <form method="post" action="/logout">
        <button type="submit">Sign out</button>
      </form>
    </header>
  );
}

export function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.92V21h2v-3.08A7 7 0 0 0 19 11h-2Z" />
    </svg>
  );
}
