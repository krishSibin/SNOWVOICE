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

