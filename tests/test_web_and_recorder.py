"""The web app's two logins, the ops console's data, and the recorder that
feeds it from the ticket agent without changing what the agent does."""

from __future__ import annotations

import base64
import json
import logging

from fastapi.testclient import TestClient

from sn_agent.agent.schemas import ConfidenceReport, Outcome, RunResult
from sn_agent.config import Settings
from sn_agent.notify.base import PublishResult
from sn_agent.observability import ticket_context
from sn_agent.recorder import RecordingNotifier, StepLog
from sn_agent.store import Store
from sn_agent.web import app as web
from sn_agent.web.auth import Identity, Signer, check_password, hash_password


def make_client(store: Store) -> TestClient:
    web.configure(Settings(app_secret_key="test-secret"), store=store)
    return TestClient(web.app)


def login(client: TestClient, email: str, password: str):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def seeded_store() -> Store:
    store = Store(":memory:")
    store.put_user("ana@example.com", "Ana", "user", hash_password("employee-pass"))
    store.put_user("ops@example.com", "Ops", "ops", hash_password("ops-pass-123"))
    return store


def test_passwords_are_hashed_and_cookies_cannot_be_forged():
    stored = hash_password("s3cret-pass")
    assert "s3cret" not in stored and check_password("s3cret-pass", stored) and not check_password("wrong", stored)
    token = Signer("key").issue(Identity("a@x.com", "A", "user"))
    payload, signature = token.rsplit(".", 1)
    assert Signer("key").read(token).email == "a@x.com"
    assert Signer("other-key").read(token) is None
    # An employee promoting themselves to ops by editing the cookie.
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["r"] = "ops"
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    assert Signer("key").read(f"{forged}.{signature}") is None


def test_an_employee_reaches_only_the_chatbot():
    client = make_client(seeded_store())

    assert login(client, "ana@example.com", "nope").headers["location"] == "/login?failed=1"
    response = login(client, "ana@example.com", "employee-pass")
    assert response.headers["location"] == "/chat"

    assert client.get("/chat").status_code == 200
    assert client.get("/ops", follow_redirects=False).headers["location"] == "/chat"
    assert client.get("/api/ops/dashboard").status_code == 404
    assert client.get("/api/ops/runs").status_code == 404


def test_ops_sees_the_console_and_everyones_conversations():
    store = seeded_store()
    store.save_conversation({"id": "c1", "user_email": "ana@example.com", "outcome": "solved", "summary": "VPN 809"})
    store.save_conversation({"id": "c2", "user_email": "bo@example.com", "outcome": "ticket_raised", "priority": 3,
                             "assignment_group": "Network Support", "incident_number": "INC1"})
    client = make_client(store)
    login(client, "ops@example.com", "ops-pass-123")

    assert client.get("/ops").status_code == 200
    assert {c["id"] for c in client.get("/api/ops/conversations").json()} == {"c1", "c2"}
    board = client.get("/api/ops/dashboard").json()
    assert board["deflection_rate"] == 0.5 and board["raised_by_group"] == {"Network Support": 1}


def test_employees_see_only_their_own_conversations():
    store = seeded_store()
    store.save_conversation({"id": "c1", "user_email": "ana@example.com", "outcome": "solved"})
    store.save_conversation({"id": "c2", "user_email": "bo@example.com", "outcome": "solved"})
    client = make_client(store)
    login(client, "ana@example.com", "employee-pass")

    assert [c["id"] for c in client.get("/api/me/conversations").json()] == ["c1"]


def test_livekit_token_carries_who_is_calling():
    from livekit import api

    client = make_client(seeded_store())
    login(client, "ana@example.com", "employee-pass")

    data = client.post("/api/livekit/token").json()

    claims = api.TokenVerifier("devkey", "secret").verify(data["token"])
    assert claims.video.room == data["room"] and claims.video.room_join
    assert '"email": "ana@example.com"' in claims.metadata


class _Notifier:
    channel = "servicenow_work_note"

    def publish(self, result, *, previous_run_id="", fingerprint=""):
        logging.getLogger("sn_agent.notify.servicenow_note").info("work note posted (run %s)", result.run_id)
        return PublishResult(channel=self.channel, delivered=True, body="[AI Resolution Assistant] note")


def test_recorder_keeps_the_run_and_its_steps_for_the_console():
    store = Store(":memory:")
    handler = StepLog(store)
    logger = logging.getLogger("sn_agent")
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        notifier = RecordingNotifier(_Notifier(), store)
        result = RunResult(
            run_id="run-1", ticket_number="INC0042", ticket_sys_id="sys-42", outcome=Outcome.RESOLUTION_FOUND,
            confidence=ConfidenceReport(score=81, level="high"), latency_s=1.0,
        )
        with ticket_context("INC0042"):
            logging.getLogger("sn_agent.agent.pipeline").info("step 1: reading the ticket to extract the facts")
            published = notifier.publish(result)
        logging.getLogger("sn_agent.agent.pipeline").info("a line outside any ticket is not kept")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    assert published.delivered  # the real notifier's answer comes back untouched
    run = store.get_run("run-1")
    assert run["outcome"] == "RESOLUTION_FOUND" and run["confidence"] == 81
    assert run["result"]["note"].startswith("[AI Resolution Assistant]")
    assert [s["message"] for s in run["steps"]] == [
        "step 1: reading the ticket to extract the facts",
        "work note posted (run run-1)",
    ]
