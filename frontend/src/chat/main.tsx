import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import type React from "react";
import { createRoot } from "react-dom/client";
import "../shared/styles.css";
import { getJSON, type Me } from "../shared/api";
import { MicIcon, Pill, TopBar, type Tone } from "../shared/ui";
import { AudioMeter } from "./AudioMeter";
import { useVoiceChat, type Message } from "./useVoiceChat";

interface PastConversation {
  id: string;
  summary?: string;
  outcome: string;
  incident_number?: string;
  started_at: number;
}

const OUTCOME: Record<string, [string, Tone]> = {
  solved: ["Solved", "good"],
  ticket_raised: ["Ticket raised", "warn"],
  ticket_drafted: ["Passed to IT", "warn"],
  abandoned: ["Ended", ""],
  open: ["In progress", ""],
};

function History({ rows }: { rows: PastConversation[] | null }) {
  if (!rows?.length) return <li className="muted">None yet.</li>;
  return (
    <>
      {rows.slice(0, 12).map((row) => {
        const [label, tone] = OUTCOME[row.outcome] || [row.outcome, ""];
        return (
          <li key={row.id}>
            <div>{row.summary || "Conversation"}</div>
            <div className="muted">
              <Pill tone={tone}>{row.incident_number ? `${label}: ${row.incident_number}` : label}</Pill>{" "}
              {new Date(row.started_at * 1000).toLocaleString()}
            </div>
          </li>
        );
      })}
    </>
  );
}

function Bubble({ m }: { m: Message }) {
  const thinking = m.role === "agent" && m.interim && !m.text;
  return (
    <div className={`bubble ${m.role}${m.role === "user" && m.interim ? " interim" : ""}`}>
      {thinking ? (
        <span className="typing" aria-label="The assistant is replying">
          <i />
          <i />
          <i />
        </span>
      ) : (
        m.text
      )}
    </div>
  );
}

function ChatPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [history, setHistory] = useState<PastConversation[] | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);

  const loadHistory = useCallback(() => {
    getJSON<PastConversation[]>("/api/me/conversations").then(setHistory, () => undefined);
  }, []);

  const chat = useVoiceChat(loadHistory);
  const { messages, status, mode, micOn, muted, pushToTalk, holding } = chat;
  const voice = mode === "livekit";
  const replying = voice && (status.state === "speaking" || status.state === "thinking");
  const statusText =
    voice && pushToTalk && !holding && status.state === "listening" && !status.text.startsWith("Hold")
      ? "Hold the mic button or Space to talk"
      : status.text;

  // Hold to talk: the mic buttons and the Space bar (not while typing).
  const holdProps = pushToTalk
    ? {
        onPointerDown: (e: React.PointerEvent) => {
          e.preventDefault();
          chat.startHold();
        },
        onPointerUp: () => void chat.endHold(),
        onPointerLeave: () => void chat.endHold(),
        onPointerCancel: () => void chat.endHold(),
        onContextMenu: (e: React.MouseEvent) => e.preventDefault(),
      }
    : { onClick: () => void chat.toggleMic() };

  useEffect(() => {
    if (!pushToTalk) return;
    const typing = (e: KeyboardEvent) => (e.target as HTMLElement | null)?.closest("input, textarea, select, button:not(.mic)");
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || e.repeat || typing(e)) return;
      e.preventDefault();
      chat.startHold();
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space" || typing(e)) return;
      e.preventDefault();
      void chat.endHold();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, [pushToTalk, chat.startHold, chat.endHold]);

  useEffect(() => {
    fetch("/api/me").then(async (r) => {
      if (!r.ok) {
        location.href = "/login";
        return;
      }
      setMe((await r.json()) as Me);
      loadHistory();
    });
  }, [loadHistory]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") void chat.stopSpeaking();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [chat.stopSpeaking]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    setSending(true);
    try {
      await chat.send(text);
    } finally {
      setSending(false);
      input.current?.focus();
    }
  }

  async function newConversation() {
    await chat.endConversation();
    location.reload();
  }

  if (!me) return null;

  return (
    <>
      <TopBar brand="IT Help Desk" who={me.name || me.email}>
        {me.role === "ops" && <a href="/ops">Ops console</a>}
      </TopBar>

      <div className="chat-layout">
        <section className="card chat" aria-label="Conversation with the help desk assistant">
          <div className="chat-head">
            <span className={`status-dot ${status.state}`} />
            <strong>Help desk assistant</strong>
            {voice && <AudioMeter track={chat.agentAudio} label="The assistant's voice level" />}
            <span className="muted small" role="status">
              {statusText}
            </span>
            <span className="spacer" style={{ flex: 1 }} />
            <label className="ptt small" title="For noisy rooms: the assistant only hears you while you hold the mic button or Space">
              <input type="checkbox" checked={pushToTalk} onChange={(e) => chat.setPushToTalk(e.target.checked)} />
              Hold to talk
            </label>
            {replying && (
              <button onClick={() => void chat.stopSpeaking()} title="Stop the assistant talking (Esc)">
                Stop
              </button>
            )}
            {chat.agentAudio && (
              <button onClick={chat.toggleMute} title="Mute the assistant's voice">
                {muted ? "Unmute voice" : "Mute voice"}
              </button>
            )}
            <button onClick={() => void newConversation()} title="Start over">
              New conversation
            </button>
          </div>

          <div className="messages" ref={scroller} aria-live="polite">
            {messages.length === 0 && (
              <div className="start">
                <button className={`mic${holding ? " live" : ""}`} aria-label={pushToTalk ? "Hold to talk" : "Start talking"} {...holdProps}>
                  <MicIcon />
                </button>
                <div>
                  {pushToTalk ? (
                    <><strong>Hold the button</strong> (or Space) while you talk, or type below.</>
                  ) : (
                    <><strong>Tap to talk</strong> to the assistant, or type below.</>
                  )}
                </div>
                <div className="muted small">
                  It checks how the same problem was fixed before. If that doesn't help, it raises a ticket for you.
                </div>
              </div>
            )}
            {messages.map((m) => (
              <Bubble key={m.id} m={m} />
            ))}
          </div>

          <form className="composer" onSubmit={(e) => void submit(e)} autoComplete="off">
            <div className="mic-wrap">
              <button
                className={`mic${(pushToTalk ? holding : micOn) ? " live" : ""}`}
                type="button"
                {...holdProps}
                aria-label={pushToTalk ? "Hold to talk" : micOn ? "Turn the microphone off" : "Turn the microphone on"}
                aria-pressed={pushToTalk ? holding : micOn}
                title={pushToTalk ? "Hold while you talk (or hold Space)" : "Microphone"}
              >
                <MicIcon />
              </button>
              {micOn && (!pushToTalk || holding) && <AudioMeter track={chat.micAudio} mine label="Your microphone level" />}
            </div>
            <input
              ref={input}
              type="text"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Describe the problem..."
              aria-label="Message"
              maxLength={2000}
            />
            <button className="primary" type="submit" disabled={sending}>
              Send
            </button>
          </form>
        </section>

        <aside className="card side">
          <h2>Your recent requests</h2>
          <ul>
            <History rows={history} />
          </ul>
        </aside>
      </div>
    </>
  );
}

createRoot(document.getElementById("root")!).render(<ChatPage />);
