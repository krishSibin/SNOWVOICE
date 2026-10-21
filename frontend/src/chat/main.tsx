import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";
import type React from "react";
import { createRoot } from "react-dom/client";
import {
  ArrowLeft,
  ArrowUp,
  AudioLines,
  Mic,
  MicOff,
  PanelLeft,
  Plus,
  Snowflake,
  Square,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import "../shared/styles.css";
import "./chat.css";
import { getJSON, type Me } from "../shared/api";
import { Pill, type Tone } from "../shared/ui";
import { AudioMeter } from "./AudioMeter";
import { VoiceOrb } from "./VoiceOrb";
import { useVoiceChat, type Message } from "./useVoiceChat";

interface PastConversation {
  id: string;
  summary?: string;
  outcome: string;
  incident_number?: string;
  started_at: number;
}

interface PastDetail extends PastConversation {
  channel?: string;
  transcript: { role: string; text: string; at?: number }[];
}

const OUTCOME: Record<string, [string, Tone]> = {
  solved: ["Solved", "good"],
  ticket_raised: ["Ticket raised", "warn"],
  ticket_drafted: ["Passed to IT", "warn"],
  abandoned: ["Ended", ""],
  open: ["In progress", ""],
};

function greeting(name: string) {
  const h = new Date().getHours();
  const part = h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  const first = name.split(/[\s@.]/)[0];
  return first ? `${part}, ${first[0].toUpperCase()}${first.slice(1)}` : part;
}

function outcomeLabel(row: PastConversation): [string, Tone] {
  const [label, tone] = OUTCOME[row.outcome] || [row.outcome, ""];
  return [row.incident_number ? `${label}: ${row.incident_number}` : label, tone];
}

function History({
  rows,
  selected,
  onOpen,
}: {
  rows: PastConversation[] | null;
  selected: string | null;
  onOpen: (id: string) => void;
}) {
  if (!rows?.length) return <li className="muted small">No requests yet.</li>;
  return (
    <>
      {rows.slice(0, 20).map((row) => {
        const [label, tone] = outcomeLabel(row);
        return (
          <li key={row.id}>
            <button
              className={`h-item${row.id === selected ? " active" : ""}`}
              onClick={() => onOpen(row.id)}
              aria-current={row.id === selected ? "true" : undefined}
            >
              <span className="h-title">{row.summary || "Conversation"}</span>
              <span className="h-meta">
                <Pill tone={tone}>{label}</Pill>
                <span>{new Date(row.started_at * 1000).toLocaleDateString()}</span>
              </span>
            </button>
          </li>
        );
      })}
    </>
  );
}

// A past conversation, read only. The URL hash (#c=<id>) holds which one, so
// the browser's back button returns to the live conversation.
function PastView({ id, onBack }: { id: string; onBack: () => void }) {
  const [conv, setConv] = useState<PastDetail | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setConv(null);
    setFailed(false);
    getJSON<PastDetail>(`/api/me/conversations/${encodeURIComponent(id)}`).then(setConv, () => setFailed(true));
  }, [id]);

  const turns: Message[] = (conv?.transcript || []).map((t, i) => ({
    id: i,
    role: t.role === "user" ? "user" : "agent",
    text: t.text,
  }));
  const [label, tone] = conv ? outcomeLabel(conv) : ["", "" as Tone];

  return (
    <>
      <div className="past-bar">
        <button className="chip" onClick={onBack}>
          <ArrowLeft size={14} /> Back to current conversation
        </button>
        {conv && (
          <span className="past-meta">
            <Pill tone={tone}>{label}</Pill>
            <span>
              {new Date(conv.started_at * 1000).toLocaleString()}
              {conv.channel ? ` · ${conv.channel}` : ""}
            </span>
          </span>
        )}
      </div>
      <div className="thread">
        <div className="thread-inner">
          {conv?.summary && <h2 className="past-title">{conv.summary}</h2>}
          {failed && <div className="turn system">That conversation couldn't be loaded.</div>}
          {!conv && !failed && <div className="turn system">Loading...</div>}
          {conv && turns.length === 0 && <div className="turn system">Nothing was said in this conversation.</div>}
          {turns.map((m) => (
            <Turn key={m.id} m={m} />
          ))}
        </div>
      </div>
      <div className="dock past-foot muted small">This is a past conversation. Go back to keep talking to SNOW AGENT.</div>
    </>
  );
}

function hashConversation(): string | null {
  return new URLSearchParams(location.hash.slice(1)).get("c");
}

function Turn({ m }: { m: Message }) {
  if (m.role === "system") return <div className="turn system">{m.text}</div>;
  if (m.role === "user") {
    return (
      <div className={`turn user${m.interim ? " interim" : ""}`}>
        <div className="user-text">{m.text}</div>
      </div>
    );
  }
  const thinking = m.interim && !m.text;
  return (
    <div className="turn agent">
      <span className={`avatar${m.interim ? " busy" : ""}`} aria-hidden>
        <Snowflake size={16} />
      </span>
      <div className="agent-text">
        {thinking ? (
          <span className="thinking" aria-label="The assistant is thinking">
            Thinking<i>.</i>
            <i>.</i>
            <i>.</i>
          </span>
        ) : (
          <>
            {m.text}
            {m.interim && <span className="caret" aria-hidden />}
          </>
        )}
      </div>
    </div>
  );
}

function ChatPage() {
  const [me, setMe] = useState<Me | null>(null);
  const [history, setHistory] = useState<PastConversation[] | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sidebar, setSidebar] = useState(() => window.innerWidth > 900);
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [viewing, setViewing] = useState<string | null>(hashConversation);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);

  const loadHistory = useCallback(() => {
    getJSON<PastConversation[]>("/api/me/conversations").then(setHistory, () => undefined);
  }, []);

  const chat = useVoiceChat(loadHistory);
  const { messages, status, mode, micOn, muted, pushToTalk, holding } = chat;
  const voice = mode === "livekit";
  const replying = voice && (status.state === "speaking" || status.state === "thinking");
  const statusText =
    voice && pushToTalk && !holding && status.state === "listening" && !status.text.startsWith("Hold")
      ? "Hold the button or Space to talk"
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
    const sync = () => setViewing(hashConversation());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  function openPast(id: string) {
    if (id !== viewing) location.hash = `c=${encodeURIComponent(id)}`;
    if (window.innerWidth <= 900) setSidebar(false);
  }

  function backToCurrent() {
    // Drop the hash without adding another history entry.
    window.history.replaceState(null, "", location.pathname + location.search);
    setViewing(null);
  }

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
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages]);

  // The message box grows with what's typed, up to a limit.
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  async function sendText(text: string) {
    text = text.trim();
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

  function submit(event: FormEvent) {
    event.preventDefault();
    void sendText(draft);
  }

  function onComposerKey(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void sendText(draft);
    }
  }

  function openVoice() {
    setVoiceOpen(true);
    if (!pushToTalk && !micOn) void chat.toggleMic();
  }

  function closeVoice() {
    setVoiceOpen(false);
    if (micOn && !pushToTalk) void chat.toggleMic();
  }

  async function newConversation() {
    // Ending the old session is best effort: a stuck disconnect or a session
    // the server no longer knows must not keep the user from starting again.
    try {
      await Promise.race([chat.endConversation(), new Promise((done) => setTimeout(done, 2000))]);
    } catch (err) {
      console.warn("could not end the conversation cleanly:", err);
    }
    // A fresh page without the #c= of a past conversation; reload() kept it,
    // so the page came back showing that same past conversation.
    location.replace(location.pathname + location.search);
  }

  if (!me) return null;

  const empty = messages.length === 0;
  const lastUser = [...messages].reverse().find((m) => m.role === "user");
  const lastAgent = [...messages].reverse().find((m) => m.role === "agent");

  const composer = (
    <form className="composer" onSubmit={submit} autoComplete="off">
      <textarea
        ref={input}
        rows={1}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onComposerKey}
        placeholder={empty ? "How can I help with your IT problem?" : "Reply to SNOW AGENT..."}
        aria-label="Message"
        maxLength={2000}
        autoFocus
      />
      <div className="composer-row">
        <button
          className={`icon-btn mic${(pushToTalk ? holding : micOn) ? " live" : ""}`}
          type="button"
          {...holdProps}
          aria-label={pushToTalk ? "Hold to talk" : micOn ? "Turn the microphone off" : "Turn the microphone on"}
          aria-pressed={pushToTalk ? holding : micOn}
          title={pushToTalk ? "Hold while you talk (or hold Space)" : "Dictate"}
        >
          {micOn && !pushToTalk ? <MicOff size={18} /> : <Mic size={18} />}
        </button>
        {micOn && (!pushToTalk || holding) && <AudioMeter track={chat.micAudio} mine label="Your microphone level" />}
        <span className="grow" />
        <label className="ptt" title="For noisy rooms: the assistant only hears you while you hold the mic button or Space">
          <input type="checkbox" checked={pushToTalk} onChange={(e) => chat.setPushToTalk(e.target.checked)} />
          Hold to talk
        </label>
        {draft.trim() ? (
          <button className="send" type="submit" disabled={sending} aria-label="Send">
            <ArrowUp size={18} />
          </button>
        ) : (
          <button className="send voice" type="button" onClick={openVoice} aria-label="Start voice mode" title="Voice mode">
            <AudioLines size={18} />
          </button>
        )}
      </div>
    </form>
  );

  return (
    <div className={`app${sidebar ? " with-side" : ""}`}>
      <aside className="sidebar" aria-label="Your recent requests" inert={!sidebar}>
        <div className="side-top">
          <span className="logo">
            <Snowflake size={18} /> SNOW AGENT
          </span>
          <button className="icon-btn" onClick={() => setSidebar(false)} aria-label="Close sidebar">
            <PanelLeft size={18} />
          </button>
        </div>
        <button className="new-chat" onClick={() => void newConversation()}>
          <Plus size={16} /> New conversation
        </button>
        <h2>Recent requests</h2>
        <ul className="history">
          <History rows={history} selected={viewing} onOpen={openPast} />
        </ul>
        <div className="side-foot">
          <span className="who-avatar">{(me.name || me.email)[0].toUpperCase()}</span>
          <span className="who-name">{me.name || me.email}</span>
          {me.role === "ops" && <a href="/ops">Ops</a>}
          <form method="post" action="/logout">
            <button type="submit" className="link">
              Sign out
            </button>
          </form>
        </div>
      </aside>

      <main className="main">
        <header className="main-head">
          {!sidebar && (
            <button className="icon-btn" onClick={() => setSidebar(true)} aria-label="Open sidebar">
              <PanelLeft size={18} />
            </button>
          )}
          <span className={`status-dot ${status.state}`} />
          <span className="muted small" role="status">
            {statusText}
          </span>
          {voice && <AudioMeter track={chat.agentAudio} label="The assistant's voice level" />}
          <span className="grow" />
          {replying && (
            <button className="chip" onClick={() => void chat.stopSpeaking()} title="Stop the assistant talking (Esc)">
              <Square size={12} /> Stop
            </button>
          )}
          {chat.agentAudio && (
            <button className="icon-btn" onClick={chat.toggleMute} aria-label={muted ? "Unmute voice" : "Mute voice"} title={muted ? "Unmute voice" : "Mute voice"}>
              {muted ? <VolumeX size={18} /> : <Volume2 size={18} />}
            </button>
          )}
        </header>

        {viewing ? (
          <PastView id={viewing} onBack={backToCurrent} />
        ) : empty ? (
          <div className="welcome">
            <h1>
              <Snowflake className="hero-flake" size={34} aria-hidden />
              {greeting(me.name || me.email)}
            </h1>
            {composer}
          </div>
        ) : (
          <>
            <div className="thread" ref={scroller} aria-live="polite">
              <div className="thread-inner">
                {messages.map((m) => (
                  <Turn key={m.id} m={m} />
                ))}
              </div>
            </div>
            <div className="dock">{composer}</div>
          </>
        )}
      </main>

      {voiceOpen && (
        <div className={`voice-mode ${status.state}`} role="dialog" aria-label="Voice conversation">
          <button className="icon-btn close" onClick={closeVoice} aria-label="Leave voice mode">
            <X size={22} />
          </button>
          <div className="voice-stage">
            <VoiceOrb state={status.state} agentTrack={chat.agentAudio} micTrack={chat.micAudio} />
            <p className="voice-status" role="status">
              {statusText}
            </p>
            <div className="captions" aria-live="polite">
              {lastUser && <p className="cap-user">{lastUser.text}</p>}
              {lastAgent?.text && <p className="cap-agent">{lastAgent.text}</p>}
            </div>
          </div>
          <div className="voice-controls">
            <button
              className={`round mic${(pushToTalk ? holding : micOn) ? " live" : ""}`}
              {...holdProps}
              aria-label={pushToTalk ? "Hold to talk" : micOn ? "Mute microphone" : "Unmute microphone"}
            >
              {micOn || pushToTalk ? <Mic size={24} /> : <MicOff size={24} />}
            </button>
            {replying && (
              <button className="round" onClick={() => void chat.stopSpeaking()} aria-label="Stop the assistant talking">
                <Square size={20} />
              </button>
            )}
            {chat.agentAudio && (
              <button className="round" onClick={chat.toggleMute} aria-label={muted ? "Unmute voice" : "Mute voice"}>
                {muted ? <VolumeX size={22} /> : <Volume2 size={22} />}
              </button>
            )}
            <button className="round end" onClick={closeVoice} aria-label="End voice mode">
              <X size={24} />
            </button>
          </div>
          {pushToTalk && <p className="muted small">Hold the mic button or Space while you talk.</p>}
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<ChatPage />);
