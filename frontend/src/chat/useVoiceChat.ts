// The conversation, voice first: it runs in a LiveKit room, where the assistant
// listens, speaks, and also accepts typed messages. If the LiveKit server can't
// be reached, typing still works through the app's own streaming endpoint.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Room,
  RoomEvent,
  Track,
  type Participant,
  type RemoteTrack,
  type TextStreamReader,
} from "livekit-client";

export type Role = "user" | "agent" | "system";
export interface Message {
  id: number;
  role: Role;
  text: string;
  /** The user's words while still being heard, or a reply still being written. */
  interim?: boolean;
}
export type Mode = "none" | "livekit" | "text";
export type AgentState = "" | "initializing" | "listening" | "thinking" | "speaking" | "idle";

const STATUS: Record<string, string> = {
  initializing: "Connecting...",
  listening: "Listening",
  thinking: "Thinking...",
  speaking: "Speaking",
  idle: "Ready",
};

// The browser's own clean-up of the microphone: no echo of the assistant's
// voice, less fan and keyboard noise. Automatic gain stays off: it turns a
// quiet room up between sentences, lifting voices across the room to the
// level of the caller's own, which the server's noise gate would then let in.
const MIC_OPTIONS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: false,
  voiceIsolation: true,
};

const CONNECT_TIMEOUT_MS = 15_000;
const AGENT_WAIT_MS = 15_000;
// A tap shorter than this on the hold-to-talk button is a slip, not speech.
const MIN_HOLD_MS = 250;
const PTT_KEY = "sn-agent.pushToTalk";

function savedPushToTalk(): boolean {
  try {
    return localStorage.getItem(PTT_KEY) === "1";
  } catch {
    return false;
  }
}

function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export function useVoiceChat(onConversationChanged: () => void) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [status, setStatusState] = useState<{ state: AgentState; text: string }>({ state: "", text: "Not connected" });
  const [mode, setModeState] = useState<Mode>("none");
  const [micOn, setMicOnState] = useState(false);
  const [muted, setMutedState] = useState(false);
  const [agentAudio, setAgentAudio] = useState<MediaStreamTrack | null>(null);
  const [micAudio, setMicAudio] = useState<MediaStreamTrack | null>(null);
  const [pushToTalk, setPushToTalkState] = useState(savedPushToTalk);
  const [holding, setHolding] = useState(false);

  const room = useRef<Room | null>(null);
  const modeRef = useRef<Mode>("none");
  const micOnRef = useRef(false);
  const mutedRef = useRef(false);
  const agent = useRef<Participant | null>(null);
  const textChatId = useRef<string | null>(null);
  const connecting = useRef<Promise<void> | null>(null);
  const lastAgentState = useRef<AgentState>("");
  const segments = useRef(new Map<string, number>()); // segment id -> message id
  const pendingUser = useRef<number | null>(null); // the user's phrase still being heard
  const agentBase = useRef(new Map<number, string>()); // reply text from earlier streams
  const audioEls = useRef(new Set<HTMLMediaElement>());
  const nextId = useRef(1);
  const pttRef = useRef(pushToTalk);
  const pttSent = useRef(false); // what the assistant was last told; it starts with hold to talk off
  const holdingRef = useRef(false);
  const holdStart = useRef<{ at: number; ready: Promise<boolean> } | null>(null);
  const changed = useRef(onConversationChanged);
  changed.current = onConversationChanged;

  // --- message list -----------------------------------------------------------

  const add = useCallback((role: Role, text: string, interim = false) => {
    const id = nextId.current++;
    setMessages((list) => [...list, { id, role, text, interim }]);
    return id;
  }, []);

  const patch = useCallback((id: number, change: Partial<Message> | ((m: Message) => Partial<Message>)) => {
    setMessages((list) =>
      list.map((m) => (m.id === id ? { ...m, ...(typeof change === "function" ? change(m) : change) } : m))
    );
  }, []);

  // A phrase whose final words never came (whisper decided it was noise).
  const dropPendingUser = useCallback(() => {
    const id = pendingUser.current;
    pendingUser.current = null;
    if (id != null) setMessages((list) => list.filter((m) => !(m.id === id && m.interim)));
  }, []);

  const setMode = (m: Mode) => {
    modeRef.current = m;
    setModeState(m);
  };
  const setMic = (on: boolean) => {
    micOnRef.current = on;
    setMicOnState(on);
  };
  const setStatus = useCallback((state: AgentState, text?: string) => {
    setStatusState({ state, text: text || STATUS[state] || state });
  }, []);

  // --- LiveKit ----------------------------------------------------------------

  const onTranscription = useCallback(
    async (reader: TextStreamReader, sender: { identity: string }) => {
      const r = room.current;
      if (!r) return;
      const attrs = reader.info.attributes || {};
      const segment = attrs["lk.segment_id"] || reader.info.id;
      const isUser = sender.identity === r.localParticipant.identity;
      const final = attrs["lk.transcription_final"] === "true";

      let id = segments.current.get(segment);
      if (id == null && isUser && pendingUser.current != null) {
        // One phrase, one bubble: the words so far and the final words can come
        // under different segment ids; keep them in the bubble already showing.
        id = pendingUser.current;
        segments.current.set(segment, id);
      }
      if (id == null && !isUser) dropPendingUser(); // the reply has started; leftovers were noise
      if (id == null) {
        id = add(isUser ? "user" : "agent", "", isUser ? !final : true);
        segments.current.set(segment, id);
      }
      if (isUser) pendingUser.current = final ? null : id;

      // The user's words come as a fresh full guess each time (replace it); the
      // assistant's reply streams in pieces (append them).
      const msgId = id;
      const base = agentBase.current.get(msgId) || "";
      let text = "";
      for await (const chunk of reader) {
        text += chunk;
        if (isUser) {
          if (text.trim()) patch(msgId, { text });
        } else if (!final) {
          patch(msgId, { text: base + text });
        }
      }
      if (final) {
        patch(msgId, (m) => ({ text: text || m.text, interim: false }));
        if (pendingUser.current === msgId) pendingUser.current = null;
        if (!isUser) agentBase.current.delete(msgId);
      } else if (isUser) {
        patch(msgId, { interim: true });
      } else {
        agentBase.current.set(msgId, base + text);
      }
    },
    [add, patch, dropPendingUser]
  );

  const waitForAgent = useCallback(async () => {
    const until = Date.now() + AGENT_WAIT_MS;
    while (!agent.current && room.current && Date.now() < until) {
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    return agent.current;
  }, []);

  const rpc = useCallback(async (method: string, payload = "") => {
    const r = room.current;
    const a = agent.current;
    if (!r || !a) throw new Error("the assistant isn't in the call");
    return r.localParticipant.performRpc({ destinationIdentity: a.identity, method, payload });
  }, []);

  // Keeps the assistant's turn handling in step with the page's switch.
  async function syncPushToTalk() {
    const want = pttRef.current;
    if (pttSent.current === want || !agent.current) return;
    try {
      await rpc("set_push_to_talk", want ? "on" : "off");
      pttSent.current = want;
    } catch (err) {
      console.warn("could not switch hold to talk:", err);
    }
  }

  const dropRoom = useCallback(() => {
    // Forget a room that never connected, without its Disconnected handler
    // resetting the text chat that carries on.
    const r = room.current;
    room.current = null;
    if (!r) return;
    r.removeAllListeners();
    void r.disconnect();
  }, []);

  const connectLiveKit = useCallback(async () => {
    setStatus("initializing", "Connecting to voice...");
    const response = await fetch("/api/livekit/token", { method: "POST" });
    if (!response.ok) throw new Error(`token request failed: ${response.status}`);
    const { url, token } = (await response.json()) as { url: string; token: string };

    const r = new Room({ adaptiveStream: true, dynacast: true, audioCaptureDefaults: MIC_OPTIONS });
    room.current = r;
    r.registerTextStreamHandler("lk.transcription", (reader, info) => void onTranscription(reader, info));

    r.on(RoomEvent.TrackSubscribed, (track: RemoteTrack) => {
      if (track.kind !== Track.Kind.Audio) return;
      const el = track.attach();
      el.muted = mutedRef.current;
      audioEls.current.add(el);
      document.body.appendChild(el);
      setAgentAudio(track.mediaStreamTrack);
    });
    r.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
      track.detach().forEach((el) => {
        audioEls.current.delete(el);
        el.remove();
      });
      if (track.kind === Track.Kind.Audio) setAgentAudio(null);
    });
    r.on(RoomEvent.ParticipantAttributesChanged, (_changed, participant) => {
      const state = participant.attributes?.["lk.agent.state"] as AgentState | undefined;
      if (state && participant !== r.localParticipant) {
        const first = agent.current == null;
        agent.current = participant;
        if (first) void syncPushToTalk();
        lastAgentState.current = state;
        setStatus(state);
      }
    });
    // Show at once that the user is being heard; their words follow once the
    // first part of the phrase is transcribed.
    r.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
      if (!micOnRef.current || (pttRef.current && !holdingRef.current)) return;
      if (speakers.includes(r.localParticipant)) setStatus("listening", "Hearing you...");
      else if (lastAgentState.current) setStatus(lastAgentState.current);
    });
    r.on(RoomEvent.Disconnected, () => {
      room.current = null;
      agent.current = null;
      pttSent.current = false;
      setMode("none");
      setMic(false);
      setMicAudio(null);
      setAgentAudio(null);
      setStatus("", "Disconnected");
      changed.current();
    });

    // Give up after 15 s rather than leave the page waiting on a blocked port.
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await Promise.race([
        r.connect(url, token),
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(`no answer from ${url} in 15 s`)), CONNECT_TIMEOUT_MS);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
    await r.startAudio();
    setMode("livekit");
    setStatus("initializing", "Waiting for the assistant...");
    // The assistant joins only calls that start after its voice worker is
    // ready; say so rather than leave the page waiting forever.
    setTimeout(() => {
      if (room.current === r && !agent.current) {
        setStatus("", "The assistant hasn't joined");
        add(
          "system",
          "The assistant hasn't joined this call. Check that the voice worker is running, then press New conversation."
        );
      }
    }, AGENT_WAIT_MS);
  }, [onTranscription, setStatus]);

  // --- text without LiveKit ---------------------------------------------------

  const startTextChat = useCallback(async () => {
    const response = await fetch("/api/chat", { method: "POST" });
    if (!response.ok) throw new Error(`could not start a chat: ${response.status}`);
    const data = (await response.json()) as { id: string; greeting: string };
    textChatId.current = data.id;
    setMode("text");
    setStatus("idle", "Text only");
    add("agent", data.greeting);
  }, [add, setStatus]);

  const sendTextOnly = useCallback(
    async (text: string) => {
      const id = add("agent", "", true);
      setStatus("thinking");
      const response = await fetch(`/api/chat/${textChatId.current}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!response.ok || !response.body) {
        patch(id, { text: "Sorry, something went wrong. Please try again.", interim: false });
        setStatus("idle", "Text only");
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let reply = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut: number;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const line = buffer.slice(0, cut).replace(/^data: /, "");
          buffer = buffer.slice(cut + 2);
          const event = JSON.parse(line) as { text?: string; error?: string };
          if (event.text) reply += event.text;
          if (event.error) reply += "\n(Sorry, something went wrong.)";
          patch(id, { text: reply });
        }
      }
      patch(id, { interim: false });
      setStatus("idle", "Text only");
      changed.current();
    },
    [add, patch, setStatus]
  );

  // --- what the page calls ------------------------------------------------------

  const ensureConnected = useCallback(async () => {
    if (modeRef.current !== "none") return;
    if (!connecting.current) {
      connecting.current = connectLiveKit()
        .catch(async (err: unknown) => {
          console.warn("LiveKit unavailable, using text only:", err);
          add("system", `Voice couldn't connect (${errorText(err)}). Switching to text.`);
          dropRoom();
          await startTextChat();
        })
        .finally(() => {
          connecting.current = null;
        });
    }
    await connecting.current;
  }, [connectLiveKit, dropRoom, startTextChat, add]);

  const toggleMic = useCallback(async () => {
    if (modeRef.current === "text") {
      // Typing fell back to text earlier; voice may be up now, so try again.
      try {
        await connectLiveKit();
      } catch (err) {
        console.warn("LiveKit still unavailable:", err);
        dropRoom();
        setMode("text");
        setStatus("idle", "Text only");
        add("system", `Voice couldn't connect (${errorText(err)}). You can still type.`);
        return;
      }
    }
    await ensureConnected();
    const r = room.current;
    if (modeRef.current !== "livekit" || !r) return;
    const on = !micOnRef.current;
    try {
      await r.localParticipant.setMicrophoneEnabled(on, MIC_OPTIONS);
      setMic(on);
      const pub = r.localParticipant.getTrackPublication(Track.Source.Microphone);
      setMicAudio(on ? (pub?.track?.mediaStreamTrack ?? null) : null);
    } catch (err) {
      console.warn("microphone failed:", err);
      add("system", `The microphone couldn't be turned on (${errorText(err)}). Check the browser's permission.`);
    }
  }, [connectLiveKit, dropRoom, ensureConnected, add, setStatus]);

  const send = useCallback(
    async (text: string) => {
      try {
        await ensureConnected();
        add("user", text);
        if (modeRef.current === "livekit" && room.current) {
          await room.current.localParticipant.sendText(text, { topic: "lk.chat" });
        } else {
          await sendTextOnly(text);
        }
      } catch {
        add("system", "Your message couldn't be sent. Please try again.");
      }
    },
    [ensureConnected, add, sendTextOnly]
  );

  const setPushToTalk = useCallback((on: boolean) => {
    pttRef.current = on;
    setPushToTalkState(on);
    try {
      localStorage.setItem(PTT_KEY, on ? "1" : "0");
    } catch {
      // private window: the choice just isn't remembered
    }
    void syncPushToTalk();
  }, []);

  /** Hold to talk: the button or Space went down. */
  const startHold = useCallback(() => {
    if (holdingRef.current) return;
    holdingRef.current = true;
    setHolding(true);
    const ready = (async () => {
      await ensureConnected();
      const r = room.current;
      if (modeRef.current !== "livekit" || !r) return false;
      try {
        if (!micOnRef.current) {
          await r.localParticipant.setMicrophoneEnabled(true, MIC_OPTIONS);
          setMic(true);
          setMicAudio(r.localParticipant.getTrackPublication(Track.Source.Microphone)?.track?.mediaStreamTrack ?? null);
        }
        if (!(await waitForAgent())) throw new Error("the assistant didn't join");
        await syncPushToTalk();
        await rpc("start_turn");
        setStatus("listening", "Hearing you... let go when you're done");
        return true;
      } catch (err) {
        console.warn("hold to talk failed:", err);
        add("system", `Couldn't start listening (${errorText(err)}).`);
        return false;
      }
    })();
    holdStart.current = { at: Date.now(), ready };
  }, [ensureConnected, waitForAgent, rpc, add, setStatus]);

  /** Hold to talk: the button or Space came up. */
  const endHold = useCallback(async () => {
    if (!holdingRef.current) return;
    holdingRef.current = false;
    setHolding(false);
    const hold = holdStart.current;
    holdStart.current = null;
    if (!hold || !(await hold.ready)) return;
    try {
      if (Date.now() - hold.at < MIN_HOLD_MS) {
        await rpc("cancel_turn");
        setStatus("listening", "Hold the button a little longer while you talk");
      } else {
        await rpc("end_turn");
        setStatus("thinking");
      }
    } catch (err) {
      console.warn("ending the turn failed:", err);
    }
  }, [rpc, setStatus]);

  /** The Stop button and Esc: cut the reply off now, tool call or not. */
  const stopSpeaking = useCallback(async () => {
    const r = room.current;
    if (modeRef.current !== "livekit" || !r || !agent.current) return;
    setStatusState((s) => ({ ...s, state: "listening" }));
    try {
      await r.localParticipant.performRpc({
        destinationIdentity: agent.current.identity,
        method: "stop_speaking",
        payload: "",
      });
    } catch (err) {
      console.warn("stop failed:", err);
    }
  }, []);

  const toggleMute = useCallback(() => {
    const m = !mutedRef.current;
    mutedRef.current = m;
    setMutedState(m);
    audioEls.current.forEach((el) => (el.muted = m));
  }, []);

  const endConversation = useCallback(async () => {
    if (room.current) await room.current.disconnect();
    if (modeRef.current === "text" && textChatId.current) {
      await fetch(`/api/chat/${textChatId.current}/end`, { method: "POST" });
    }
  }, []);

  useEffect(() => {
    const onUnload = () => {
      if (modeRef.current === "text" && textChatId.current) navigator.sendBeacon(`/api/chat/${textChatId.current}/end`);
      void room.current?.disconnect();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, []);

  return {
    messages,
    status,
    mode,
    micOn,
    muted,
    agentAudio,
    micAudio,
    pushToTalk,
    holding,
    setPushToTalk,
    startHold,
    endHold,
    send,
    toggleMic,
    stopSpeaking,
    toggleMute,
    endConversation,
  };
}
