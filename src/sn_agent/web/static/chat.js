// The employee chatbot page.
//
// Voice first: the conversation runs in a LiveKit room, where the assistant
// listens, speaks, and also accepts typed messages. If the LiveKit server can't
// be reached, typing still works through the app's own streaming endpoint.

const { Room, RoomEvent, Track } = LivekitClient;

const $ = (id) => document.getElementById(id);
const messages = $("messages");
const STATUS = {
  initializing: "Connecting...",
  listening: "Listening",
  thinking: "Thinking...",
  speaking: "Speaking",
  idle: "Ready",
};

let mode = "none"; // none | livekit | text
let room = null;
let micOn = false;
let muted = false;
let textChatId = null;
let connecting = null;
let agent = null; // the assistant's participant in the room
const segments = new Map(); // segment id -> bubble
let pendingUser = null; // the user's bubble still showing words in progress

// A phrase whose final words never came (whisper decided it was noise).
function dropPendingUser() {
  if (pendingUser && pendingUser.classList.contains("interim")) pendingUser.remove();
  pendingUser = null;
}

function bubble(role, text, extraClass) {
  $("start")?.remove();
  const el = document.createElement("div");
  el.className = `bubble ${role}${extraClass ? " " + extraClass : ""}`;
  el.textContent = text;
  messages.appendChild(el);
  messages.scrollTop = messages.scrollHeight;
  return el;
}

function setStatus(state, text) {
  $("dot").className = `status-dot ${state || ""}`;
  $("status").textContent = text || STATUS[state] || state || "";
  $("stop").hidden = !(mode === "livekit" && (state === "speaking" || state === "thinking"));
}

// Show at once that the user is being heard; their words follow in a bubble
// once the first part of the phrase is transcribed. (A placeholder bubble
// was left behind whenever noise produced no words.)
let lastAgentState = "";
function showHearing(speaking) {
  if (speaking) setStatus("listening", "Hearing you...");
  else if (lastAgentState) setStatus(lastAgentState);
}

async function stopSpeaking() {
  if (mode !== "livekit" || !agent) return;
  $("stop").hidden = true;
  try {
    await room.localParticipant.performRpc({ destinationIdentity: agent.identity, method: "stop_speaking", payload: "" });
  } catch (err) {
    console.warn("stop failed:", err);
  }
}

function setMic(on) {
  micOn = on;
  $("mic").classList.toggle("live", on);
  $("mic").setAttribute("aria-label", on ? "Turn the microphone off" : "Turn the microphone on");
}

// --- LiveKit -----------------------------------------------------------------

async function connectLiveKit() {
  setStatus("initializing", "Connecting to voice...");
  const response = await fetch("/api/livekit/token", { method: "POST" });
  if (!response.ok) throw new Error(`token request failed: ${response.status}`);
  const { url, token } = await response.json();

  room = new Room({ adaptiveStream: true, dynacast: true });

  room.registerTextStreamHandler("lk.transcription", async (reader, sender) => {
    const attrs = reader.info.attributes || {};
    const id = attrs["lk.segment_id"] || reader.info.id;
    const isUser = sender.identity === room.localParticipant.identity;
    const final = attrs["lk.transcription_final"] === "true";
    let el = segments.get(id);
    if (!el && isUser && pendingUser) {
      // One phrase, one bubble: the words so far and the final words can come
      // under different segment ids; keep them in the bubble already showing.
      el = pendingUser;
      segments.set(id, el);
    }
    if (!el && !isUser) dropPendingUser(); // the reply has started; leftovers were noise
    if (!el) {
      el = bubble(isUser ? "user" : "agent", "", isUser && !final ? "interim" : "");
      segments.set(id, el);
    }
    if (isUser) pendingUser = final ? null : el;

    // The user's words come as a fresh full guess each time (replace it); the
    // assistant's reply streams in pieces (append them).
    let text = "";
    for await (const chunk of reader) {
      text += chunk;
      if (isUser) {
        if (text.trim()) el.textContent = text;
      } else if (!final) {
        el.textContent = (el.dataset.base || "") + text;
      }
      messages.scrollTop = messages.scrollHeight;
    }
    if (final) {
      el.textContent = text || el.textContent;
      el.classList.remove("interim");
      if (pendingUser === el) pendingUser = null;
    } else if (isUser) {
      el.classList.add("interim");
    } else {
      el.dataset.base = el.textContent;
    }
    messages.scrollTop = messages.scrollHeight;
  });

  room.on(RoomEvent.TrackSubscribed, (track) => {
    if (track.kind === Track.Kind.Audio) {
      const el = track.attach();
      el.muted = muted;
      el.dataset.agentAudio = "1";
      document.body.appendChild(el);
      $("mute").hidden = false;
    }
  });
  room.on(RoomEvent.TrackUnsubscribed, (track) => track.detach().forEach((el) => el.remove()));
  room.on(RoomEvent.ParticipantAttributesChanged, (changed, participant) => {
    const state = participant.attributes?.["lk.agent.state"];
    if (state && participant !== room.localParticipant) {
      agent = participant;
      lastAgentState = state;
      setStatus(state);
    }
  });
  room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
    if (micOn) showHearing(speakers.includes(room.localParticipant));
  });
  room.on(RoomEvent.Disconnected, () => {
    mode = "none";
    setMic(false);
    setStatus("", "Disconnected");
    loadHistory();
  });

  // Give up after 15 s rather than leave the page waiting on a blocked port.
  await Promise.race([
    room.connect(url, token),
    new Promise((_, reject) => setTimeout(() => reject(new Error(`no answer from ${url} in 15 s`)), 15000)),
  ]);
  await room.startAudio();
  mode = "livekit";
  setStatus("initializing", "Waiting for the assistant...");
}

function dropRoom() {
  // Forget a room that never connected, without its Disconnected handler
  // resetting the text chat that carries on.
  if (!room) return;
  room.removeAllListeners();
  room.disconnect();
}

async function ensureConnected() {
  if (mode !== "none") return mode;
  if (!connecting) {
    connecting = connectLiveKit()
      .catch(async (err) => {
        console.warn("LiveKit unavailable, using text only:", err);
        bubble("system", `Voice couldn't connect (${err.message || err}). Switching to text.`);
        dropRoom();
        room = null;
        await startTextChat();
      })
      .finally(() => (connecting = null));
  }
  await connecting;
  return mode;
}

async function toggleMic() {
  if (mode === "text") {
    // Typing fell back to text earlier; voice may be up now, so try again.
    try {
      await connectLiveKit();
    } catch (err) {
      console.warn("LiveKit still unavailable:", err);
      dropRoom();
      room = null;
      mode = "text";
      setStatus("idle", "Text only");
      bubble("system", `Voice couldn't connect (${err.message || err}). You can still type.`);
      return;
    }
  }
  await ensureConnected();
  if (mode !== "livekit") return;
  try {
    await room.localParticipant.setMicrophoneEnabled(!micOn);
    setMic(!micOn);
  } catch (err) {
    console.warn("microphone failed:", err);
    bubble("system", `The microphone couldn't be turned on (${err.message || err}). Check the browser's permission.`);
  }
}

// --- text without LiveKit ----------------------------------------------------

async function startTextChat() {
  const response = await fetch("/api/chat", { method: "POST" });
  if (!response.ok) throw new Error(`could not start a chat: ${response.status}`);
  const data = await response.json();
  textChatId = data.id;
  mode = "text";
  setStatus("idle", "Text only");
  bubble("agent", data.greeting);
}

async function sendTextOnly(text) {
  const el = bubble("agent", "");
  setStatus("thinking");
  const response = await fetch(`/api/chat/${textChatId}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!response.ok || !response.body) {
    el.textContent = "Sorry, something went wrong. Please try again.";
    setStatus("idle", "Text only");
    return;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const line = buffer.slice(0, cut).replace(/^data: /, "");
      buffer = buffer.slice(cut + 2);
      const event = JSON.parse(line);
      if (event.text) el.textContent += event.text;
      if (event.error) el.textContent += "\n(Sorry, something went wrong.)";
      messages.scrollTop = messages.scrollHeight;
    }
  }
  setStatus("idle", "Text only");
  loadHistory();
}

// --- composer ------------------------------------------------------------------

$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("text");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  $("send").disabled = true;
  try {
    await ensureConnected();
    bubble("user", text);
    if (mode === "livekit") {
      await room.localParticipant.sendText(text, { topic: "lk.chat" });
    } else {
      await sendTextOnly(text);
    }
  } catch (err) {
    bubble("system", "Your message couldn't be sent. Please try again.");
  } finally {
    $("send").disabled = false;
    input.focus();
  }
});

$("mic").addEventListener("click", toggleMic);
$("startVoice").addEventListener("click", toggleMic);

$("stop").addEventListener("click", stopSpeaking);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") stopSpeaking();
});

$("mute").addEventListener("click", () => {
  muted = !muted;
  document.querySelectorAll("audio[data-agent-audio]").forEach((el) => (el.muted = muted));
  $("mute").textContent = muted ? "Unmute voice" : "Mute voice";
});

$("newChat").addEventListener("click", async () => {
  if (room) await room.disconnect();
  if (mode === "text" && textChatId) await fetch(`/api/chat/${textChatId}/end`, { method: "POST" });
  location.reload();
});

window.addEventListener("beforeunload", () => {
  if (mode === "text" && textChatId) navigator.sendBeacon(`/api/chat/${textChatId}/end`);
});

// --- side panel ----------------------------------------------------------------

const OUTCOME = {
  solved: ["Solved", "good"],
  ticket_raised: ["Ticket raised", "warn"],
  ticket_drafted: ["Passed to IT", "warn"],
  abandoned: ["Ended", ""],
  open: ["In progress", ""],
};

async function loadHistory() {
  const response = await fetch("/api/me/conversations");
  if (!response.ok) return;
  const rows = await response.json();
  const list = $("history");
  list.innerHTML = "";
  if (!rows.length) {
    list.innerHTML = '<li class="muted">None yet.</li>';
    return;
  }
  for (const row of rows.slice(0, 12)) {
    const [label, tone] = OUTCOME[row.outcome] || [row.outcome, ""];
    const li = document.createElement("li");
    const when = new Date(row.started_at * 1000).toLocaleString();
    li.innerHTML = `<div></div><div class="muted"></div>`;
    li.children[0].textContent = row.summary || "Conversation";
    const pill = document.createElement("span");
    pill.className = `pill ${tone}`;
    pill.textContent = row.incident_number ? `${label}: ${row.incident_number}` : label;
    li.children[1].append(pill, ` ${when}`);
    list.appendChild(li);
  }
}

(async function init() {
  const me = await fetch("/api/me").then((r) => (r.ok ? r.json() : null));
  if (!me) return (location.href = "/login");
  $("who").textContent = me.name || me.email;
  $("opsLink").hidden = me.role !== "ops";
  loadHistory();
})();
