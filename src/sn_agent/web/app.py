"""The web app: the employee chatbot page and the ops console, behind two
kinds of login.

    sn-agent web            # http://localhost:8080

Separate from the ticket agent's optional webhook app on purpose: the ticket
agent keeps running exactly as before (`sn-agent serve`), and this app only
reads the local record it leaves behind.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_settings
from ..recorder import open_store
from ..store import Store
from .auth import COOKIE, SESSION_SECONDS, Identity, Signer, check_password, hash_password

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
# The React pages, built from frontend/ by `npm run build`.
UI = HERE / "ui"
CHAT_IDLE_SECONDS = 30 * 60


class _State:
    settings: Settings
    store: Store
    signer: Signer
    services: Any = None  # intake services, built on first chat
    chats: dict[str, tuple[Any, float]] = {}


state = _State()


@asynccontextmanager
async def lifespan(_app: FastAPI):  # pragma: no cover - server lifecycle
    configure(get_settings())
    yield


def configure(settings: Settings, *, store: Store | None = None, services: Any = None) -> None:
    state.settings = settings
    state.store = store or open_store(settings)
    state.signer = Signer(settings.app_secret_key.get_secret_value())
    state.services = services
    state.chats = {}
    _seed_logins(settings, state.store)


def _seed_logins(settings: Settings, store: Store) -> None:
    """Create the user and admin logins named in .env, if any."""
    for email, password, role, name in (
        (settings.app_user_email, settings.app_user_password, "user", "User"),
        (settings.app_admin_email, settings.app_admin_password, "ops", "Admin"),
    ):
        secret = password.get_secret_value()
        if not email or not secret:
            continue
        existing = store.get_user(email)
        if existing and existing["role"] == role and check_password(secret, existing["password_hash"]):
            continue  # already there; skip the slow rehash
        store.put_user(email, existing["name"] if existing else name, role, hash_password(secret))


app = FastAPI(title="ServiceNow Agent", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=UI / "assets", check_dir=False), name="assets")


def _page(name: str):
    page = UI / f"{name}.html"
    if not page.exists():
        return PlainTextResponse(
            "The web pages are not built yet. Run: cd frontend && npm install && npm run build",
            status_code=503,
        )
    return FileResponse(page)


# --- identity ---------------------------------------------------------------


def current(request: Request) -> Identity | None:
    return state.signer.read(request.cookies.get(COOKIE))


def require_user(request: Request) -> Identity:
    identity = current(request)
    if identity is None:
        raise HTTPException(status_code=401, detail="log in first")
    return identity


def require_ops(identity: Identity = Depends(require_user)) -> Identity:
    if not identity.is_ops:
        # Hidden rather than forbidden, so an employee's account learns nothing.
        raise HTTPException(status_code=404)
    return identity


# --- pages ------------------------------------------------------------------


@app.get("/")
def home(request: Request):
    identity = current(request)
    if identity is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/ops" if identity.is_ops else "/chat", status_code=303)


@app.get("/login")
def login_page():
    return _page("login")


@app.post("/login")
def login(email: str = Form(...), password: str = Form(...), portal: str = Form("user")):
    # Two sign-in forms on one page: employees land on the chatbot, admins
    # (the ops role) on the ops console. The admin form refuses other roles.
    admin = portal == "admin"
    failed = "/login?failed=1" + ("&portal=admin" if admin else "")
    user = state.store.get_user(email)
    if not user or not check_password(password, user["password_hash"]):
        time.sleep(0.5)  # slows guessing a little; not a substitute for a lockout
        return RedirectResponse(failed, status_code=303)
    identity = Identity(email=user["email"], name=user["name"], role=user["role"])
    if admin and not identity.is_ops:
        return RedirectResponse("/login?denied=1&portal=admin", status_code=303)
    response = RedirectResponse("/ops" if admin else "/chat", status_code=303)
    response.set_cookie(
        COOKIE, state.signer.issue(identity), max_age=SESSION_SECONDS, httponly=True, samesite="lax"
    )
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.get("/chat")
def chat_page(request: Request):
    if current(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _page("chat")


@app.get("/ops")
def ops_page(request: Request):
    identity = current(request)
    if identity is None:
        return RedirectResponse("/login", status_code=303)
    if not identity.is_ops:
        return RedirectResponse("/chat", status_code=303)
    return _page("ops")


@app.get("/api/me")
def me(identity: Identity = Depends(require_user)) -> dict[str, Any]:
    return {"email": identity.email, "name": identity.name, "role": identity.role}


@app.get("/api/me/conversations")
def my_conversations(identity: Identity = Depends(require_user)) -> list[dict[str, Any]]:
    return state.store.list_conversations(user_email=identity.email, limit=50)


@app.get("/api/me/conversations/{conv_id}")
def my_conversation(conv_id: str, identity: Identity = Depends(require_user)) -> dict[str, Any]:
    # Someone else's conversation looks the same as a missing one.
    conv = state.store.get_conversation(conv_id)
    if conv is None or conv.get("user_email") != identity.email.lower().strip():
        raise HTTPException(status_code=404)
    keep = ("id", "channel", "started_at", "outcome", "summary", "incident_number")
    return {**{k: conv.get(k) for k in keep}, "transcript": conv.get("transcript") or []}


# --- voice and text through LiveKit ----------------------------------------


@app.post("/api/livekit/token")
def livekit_token(identity: Identity = Depends(require_user)) -> dict[str, str]:
    from livekit import api

    settings = state.settings
    room = f"intake-{uuid.uuid4().hex[:12]}"
    conversation_id = uuid.uuid4().hex[:16]
    token = (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret.get_secret_value())
        .with_identity(f"{identity.email}#{conversation_id[:6]}")
        .with_name(identity.name or identity.email)
        .with_metadata(json.dumps({"email": identity.email, "name": identity.name, "conversation_id": conversation_id}))
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
        .with_ttl(timedelta(hours=1))
        .to_jwt()
    )
    return {
        "url": settings.livekit_public_url or settings.livekit_url,
        "token": token,
        "room": room,
        "conversation_id": conversation_id,
    }


# --- text chat without LiveKit ---------------------------------------------
# The page falls back to this when the LiveKit server can't be reached, so
# typing always works even if voice is down.


def _services():
    if state.services is None:
        from ..intake.session import build_services

        state.services = build_services(state.settings, store=state.store)
    return state.services


def _sweep_idle_chats() -> None:
    cutoff = time.time() - CHAT_IDLE_SECONDS
    for chat_id, (chat, seen) in list(state.chats.items()):
        if seen < cutoff:
            chat.session.close()
            state.chats.pop(chat_id, None)


@app.post("/api/chat")
def start_chat(identity: Identity = Depends(require_user)) -> dict[str, str]:
    from ..intake.chat import TextChat, chat_client, chat_model
    from ..intake.session import IntakeSession

    _sweep_idle_chats()
    session = IntakeSession(_services(), user_email=identity.email, user_name=identity.name, channel="text")
    chat = TextChat(session, chat_client(state.settings), chat_model(state.settings))
    state.chats[session.id] = (chat, time.time())
    return {"id": session.id, "greeting": chat.greeting}


def _owned_chat(chat_id: str, identity: Identity):
    entry = state.chats.get(chat_id)
    if entry is None or entry[0].session.user_email != identity.email:
        raise HTTPException(status_code=404, detail="conversation not found; start a new one")
    state.chats[chat_id] = (entry[0], time.time())
    return entry[0]


@app.post("/api/chat/{chat_id}/message")
async def send_message(chat_id: str, request: Request, identity: Identity = Depends(require_user)):
    chat = _owned_chat(chat_id, identity)
    body = await request.json()
    text = str(body.get("text", "")).strip()[:2000]
    if not text:
        raise HTTPException(status_code=400, detail="empty message")

    async def stream():
        try:
            async for part in chat.reply(text):
                yield f"data: {json.dumps({'text': part})}\n\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("chat %s failed", chat_id)
            yield f"data: {json.dumps({'error': str(exc)[:200]})}\n\n"
        yield "data: {\"done\": true}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/chat/{chat_id}/end")
def end_chat(chat_id: str, identity: Identity = Depends(require_user)) -> dict[str, str]:
    chat = _owned_chat(chat_id, identity)
    chat.session.close()
    state.chats.pop(chat_id, None)
    return {"outcome": chat.session.outcome}


# --- ops console ------------------------------------------------------------


@app.get("/api/ops/dashboard")
def dashboard(days: int = 30, _ops: Identity = Depends(require_ops)) -> dict[str, Any]:
    return state.store.dashboard(days=max(1, min(days, 365)))


# A heartbeat older than this means `serve` stopped or crashed.
AGENT_STALE_S = 15.0


@app.get("/api/ops/agent")
def agent_live(after: int = 0, _ops: Identity = Depends(require_ops)) -> dict[str, Any]:
    """The running ticket agent: its state, what it is working on, what is
    still waiting for it, result counts, and the log lines after `after`."""

    store = state.store
    status = store.get_agent_status()
    now = time.time()
    if status:
        age = now - status["heartbeat_at"]
        alive = not status.get("stopped") and age < AGENT_STALE_S
        runner = status.get("runner") or {}
        sweep = status.get("sweep") or {}
        if not alive:
            phase = "stopped"
        elif runner.get("working"):
            phase = "working"
        elif sweep.get("sweeping"):
            phase = "sweeping"
        else:
            phase = "idle"
        status.update(alive=alive, phase=phase, heartbeat_age_s=round(age, 1))
    return {
        "now": now,
        "status": status,
        "counts": {"day": store.job_counts(now - 86400), "all": store.job_counts()},
        "recent": store.recent_jobs(20),
        "log": store.steps_after(max(0, after), limit=300),
        "instance_url": state.settings.servicenow_instance_url.rstrip("/"),
    }


@app.get("/api/ops/runs")
def runs(_ops: Identity = Depends(require_ops)) -> list[dict[str, Any]]:
    return state.store.list_runs()


@app.get("/api/ops/runs/{run_id}")
def run_detail(run_id: str, _ops: Identity = Depends(require_ops)) -> dict[str, Any]:
    run = state.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404)
    run["instance_url"] = state.settings.servicenow_instance_url.rstrip("/")
    return run


@app.get("/api/ops/conversations")
def conversations(_ops: Identity = Depends(require_ops)) -> list[dict[str, Any]]:
    return state.store.list_conversations()


@app.get("/api/ops/conversations/{conv_id}")
def conversation_detail(conv_id: str, _ops: Identity = Depends(require_ops)) -> dict[str, Any]:
    conv = state.store.get_conversation(conv_id)
    if conv is None:
        raise HTTPException(status_code=404)
    return conv


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "open_text_chats": len(state.chats)})


def run(host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("sn_agent.web.app:app", host=host, port=port)

