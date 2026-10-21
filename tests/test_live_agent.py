"""The live view of `sn-agent serve`: what the running agent publishes, and
what the ops console reads back."""

from __future__ import annotations

import logging
import time

import pytest

from sn_agent.config import Settings
from sn_agent.monitor import AgentMonitor
from sn_agent.runner import JobRunner
from sn_agent.store import Store
from sn_agent.web.auth import hash_password

from .test_queue_and_sweep import RecordingService
from .test_web_and_recorder import login, make_client


@pytest.fixture(autouse=True)
def _info_logging():
    """`serve` logs at INFO; without setup() the agent's loggers would drop it."""

    logger = logging.getLogger("sn_agent")
    previous = logger.level
    logger.setLevel(logging.INFO)
    yield
    logger.setLevel(previous)


def ops_client(store: Store):
    store.put_user("ops@example.com", "Ops", "ops", hash_password("ops-pass-123"))
    store.put_user("ana@example.com", "Ana", "user", hash_password("employee-pass"))
    client = make_client(store)
    login(client, "ops@example.com", "ops-pass-123", portal="admin")
    return client


def run_tickets(store: Store, *, fail: set[str]):
    service = RecordingService(fail=fail)
    runner = JobRunner(workers=1, service_factory=lambda: service, max_failures=1)
    monitor = AgentMonitor(runner, Settings(), store=store)
    for number in ("INC_OK", "INC_BAD"):
        runner.submit(number, priority=2)
    runner.start()
    runner.wait()
    return runner, monitor


def test_every_result_is_recorded_including_failures():
    store = Store(":memory:")

    run_tickets(store, fail={"INC_BAD"})

    assert store.job_counts() == {"posted": 1, "failed": 1}
    failed = next(j for j in store.recent_jobs() if j["status"] == "failed")
    assert failed["ticket_number"] == "INC_BAD" and "boom" in failed["detail"]


def test_heartbeat_reports_workers_queue_and_tickets_given_up_on():
    store = Store(":memory:")
    runner, monitor = run_tickets(store, fail={"INC_BAD"})

    monitor.beat()
    status = store.get_agent_status()

    assert status["runner"]["workers"] == 1
    assert status["runner"]["working"] == [] and status["runner"]["queued"] == []
    assert status["runner"]["gave_up"] == ["INC_BAD"]
    assert status["runner"]["totals"] == {"posted": 1, "failed": 1}


def test_queued_tickets_show_most_urgent_first():
    runner = JobRunner(workers=1, service_factory=RecordingService)
    runner.submit("INC_P3", priority=3)
    runner.submit("INC_P1", priority=1)

    assert [q["number"] for q in runner.snapshot()["queued"]] == ["INC_P1", "INC_P3"]


def test_sweep_lines_with_no_ticket_reach_the_live_log():
    store = Store(":memory:")
    AgentMonitor(JobRunner(workers=1, service_factory=RecordingService), Settings(), store=store)

    logging.getLogger("sn_agent.sweep").info("sweep: 3 open ticket(s), 1 need the agent")

    assert any("1 need the agent" in line["message"] for line in store.steps_after())


def test_ops_console_sees_a_running_agent_then_a_stopped_one():
    store = Store(":memory:")
    runner, monitor = run_tickets(store, fail={"INC_BAD"})
    client = ops_client(store)

    monitor.beat()
    live = client.get("/api/ops/agent").json()
    assert live["status"]["alive"] is True and live["status"]["phase"] == "idle"
    assert live["counts"]["day"] == {"posted": 1, "failed": 1}
    newest = live["log"][-1]["id"]

    monitor.stop()
    later = client.get(f"/api/ops/agent?after={newest}").json()
    assert later["status"]["alive"] is False and later["status"]["phase"] == "stopped"
    assert all(line["id"] > newest for line in later["log"])


def test_a_silent_agent_counts_as_stopped():
    store = Store(":memory:")
    store.put_agent_status({"runner": {"working": [{"number": "INC1", "since": time.time()}]}})
    with store._tx() as cur:
        cur.execute("UPDATE agent_status SET heartbeat_at = heartbeat_at - 60")
    client = ops_client(store)

    assert client.get("/api/ops/agent").json()["status"]["phase"] == "stopped"


def test_employees_cannot_see_the_live_agent():
    store = Store(":memory:")
    client = ops_client(store)
    client.cookies.clear()
    login(client, "ana@example.com", "employee-pass")

    assert client.get("/api/ops/agent").status_code == 404
