"""Create demo tickets in a ServiceNow developer instance.

This is a setup tool, not part of the agent. It writes records with an admin
account, which is exactly what the agent's own integration user must never be
able to do — hence the separate client and separate credentials here.

    python scripts/seed_demo_data.py --dry-run     # show what would be created
    python scripts/seed_demo_data.py               # create it
    python scripts/seed_demo_data.py --cleanup --yes   # delete everything it created

Every record is tagged with correlation_id = "sn-agent-demo", which is what
cleanup matches on. Nothing without that tag is ever touched.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from sn_agent.tls import ssl_context  # noqa: E402

from scripts.demo_data import (  # noqa: E402
    CHANGES,
    DEMO_TAG,
    GROUPS,
    INCIDENTS,
    MANAGER,
    PROBLEMS,
    TEAM,
    Change,
    Incident,
    Problem,
)

SN_DATETIME = "%Y-%m-%d %H:%M:%S"
STATE_RESOLVED = "6"
STATE_IN_PROGRESS = "2"


def env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


class SeedClient:
    """Minimal read/write Table API client for seeding. Basic auth with an admin
    account, because backdating tickets and creating CIs needs one."""

    def __init__(self, instance: str, user: str, password: str, *, dry_run: bool = False):
        self.instance = instance.rstrip("/")
        self.dry_run = dry_run
        self.http = httpx.Client(
            base_url=self.instance,
            auth=(user, password),
            timeout=60.0,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            # Same OS trust store the agent itself uses, so a corporate CA does
            # not fail this script with CERTIFICATE_VERIFY_FAILED.
            verify=ssl_context(),
        )
        self.created: list[tuple[str, str, str]] = []  # (table, number, sys_id)

    def query(self, table: str, query: str, *, fields: str = "sys_id,name,number", limit: int = 1) -> list[dict]:
        if self.dry_run:  # a dry run never touches the network
            return []
        response = self.http.get(
            f"/api/now/table/{table}",
            params={"sysparm_query": query, "sysparm_limit": str(limit), "sysparm_fields": fields},
        )
        response.raise_for_status()
        return response.json().get("result", [])

    def insert(self, table: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.dry_run:
            return {"sys_id": f"dry-{table}-{len(self.created)}", "number": f"DRY{len(self.created):04d}"}
        response = self.http.post(f"/api/now/table/{table}", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"insert into {table} failed: {response.status_code} {response.text[:400]}")
        return response.json()["result"]

    def patch(self, table: str, sys_id: str, payload: dict[str, Any], *, tolerate: bool = False) -> None:
        if self.dry_run:
            return
        response = self.http.patch(f"/api/now/table/{table}/{sys_id}", json=payload)
        if response.status_code >= 400 and not tolerate:
            raise RuntimeError(f"update {table}/{sys_id} failed: {response.status_code} {response.text[:400]}")

    def delete(self, table: str, sys_id: str) -> None:
        if self.dry_run:
            return
        response = self.http.delete(f"/api/now/table/{table}/{sys_id}")
        if response.status_code >= 400:
            raise RuntimeError(f"delete {table}/{sys_id} failed: {response.status_code}")

    # --- reference lookups ---------------------------------------------------

    def resolve_or_create(self, table: str, name: str, extra: dict[str, Any] | None = None) -> str:
        if not name:
            return ""
        cache_key = (table, name)
        if cache_key in _REFERENCE_CACHE:
            return _REFERENCE_CACHE[cache_key]
        rows = self.query(table, f"name={name}")
        if rows:
            sys_id = rows[0]["sys_id"]
        else:
            payload = {"name": name, **(extra or {})}
            sys_id = self.insert(table, payload)["sys_id"]
            print(f"  created {table}: {name}")
        _REFERENCE_CACHE[cache_key] = sys_id
        return sys_id

    def ensure_user(self, full_name: str, *, manager: str = "") -> str:
        """Find a user by display name, or create one. The first word is the first
        name and the rest the last name, so ServiceNow composes the same display name."""

        rows = self.query("sys_user", f"name={full_name}", fields="sys_id")
        if rows:
            sys_id = rows[0]["sys_id"]
            if manager:
                self.patch("sys_user", sys_id, {"manager": manager})
            return sys_id
        first, _, last = full_name.partition(" ")
        user_name = ".".join(full_name.lower().split())
        fields: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
            "user_name": user_name,
            "email": f"{user_name}@example.com",
            "active": "true",
        }
        if manager:
            fields["manager"] = manager
        sys_id = self.insert("sys_user", fields)["sys_id"]
        print(f"  created user: {full_name}")
        return sys_id

    def ensure_member(self, group_sys_id: str, user_sys_id: str) -> None:
        if self.query("sys_user_grmember", f"group={group_sys_id}^user={user_sys_id}", fields="sys_id"):
            return
        self.insert("sys_user_grmember", {"group": group_sys_id, "user": user_sys_id})

    def pick_users(self, count: int = 8) -> list[dict[str, str]]:
        if self.dry_run:
            return [{"sys_id": f"dry-user-{i}", "name": f"Demo User {i}"} for i in range(3)]
        rows = self.query(
            "sys_user",
            "active=true^emailISNOTEMPTY^ORDERBYname",
            fields="sys_id,name,user_name",
            limit=count,
        )
        return [{"sys_id": r["sys_id"], "name": r.get("name", "")} for r in rows]

    def group_members(self, group_sys_id: str, fallback: list[dict[str, str]]) -> list[str]:
        """Instances commonly carry a business rule requiring the assignee to be a
        member of the assignment group, so assignees are drawn from membership —
        and a member is added when a group has none."""

        if not group_sys_id:
            return []
        if group_sys_id in _MEMBER_CACHE:
            return _MEMBER_CACHE[group_sys_id]
        if self.dry_run:
            _MEMBER_CACHE[group_sys_id] = [u["sys_id"] for u in fallback]
            return _MEMBER_CACHE[group_sys_id]

        rows = self.query("sys_user_grmember", f"group={group_sys_id}", fields="sys_id,user", limit=20)
        members = [_ref(row.get("user")) for row in rows]
        members = [m for m in members if m]
        if not members and fallback:
            user = fallback[len(_MEMBER_CACHE) % len(fallback)]
            self.insert("sys_user_grmember", {"group": group_sys_id, "user": user["sys_id"]})
            print(f"  added {user['name']} to an empty group so tickets can be assigned")
            members = [user["sys_id"]]
        _MEMBER_CACHE[group_sys_id] = members
        return members


_REFERENCE_CACHE: dict[tuple[str, str], str] = {}
_MEMBER_CACHE: dict[str, list[str]] = {}


def _ref(value: Any) -> str:
    """Reference fields come back as {"value": ..., "link": ...} when fields are
    selected explicitly."""

    if isinstance(value, dict):
        return str(value.get("value") or "")
    return str(value or "")


def utc(days_ago: int = 0, hours_ago: int = 0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return moment.strftime(SN_DATETIME)


def setup_team(client: SeedClient) -> list[str]:
    """Create the demo team and its manager, and make the team members of every
    demo group, since instances commonly require an assignee to be in the group."""

    manager = client.ensure_user(MANAGER)
    team = [client.ensure_user(name, manager=manager) for name in TEAM]
    for group in GROUPS:
        group_id = client.resolve_or_create("sys_user_group", group, {"manager": manager})
        client.patch("sys_user_group", group_id, {"manager": manager})
        for member in team:
            client.ensure_member(group_id, member)
    print(f"  {', '.join(TEAM)} in {', '.join(GROUPS)}, managed by {MANAGER}")
    return team


def seed(client: SeedClient) -> dict[str, str]:
    numbers: dict[str, str] = {}
    sys_ids: dict[str, str] = {}

    print("\nSetting up the demo team...")
    team = setup_team(client)

    print("\nResolving configuration items, services and groups...")
    for incident in INCIDENTS:
        client.resolve_or_create("cmdb_ci_appl", incident.ci)
        client.resolve_or_create("cmdb_ci_service", incident.service)
        client.resolve_or_create("sys_user_group", incident.group)

    print("\nCreating problems...")
    for index, problem in enumerate(PROBLEMS):
        record = client.insert("problem", _problem_payload(client, problem, team[index % len(team)]))
        numbers[problem.key], sys_ids[problem.key] = record.get("number", ""), record["sys_id"]
        client.patch("problem", record["sys_id"], {"state": "107"}, tolerate=True)
        client.patch("problem", record["sys_id"], {"active": "false"}, tolerate=True)
        client.created.append(("problem", numbers[problem.key], record["sys_id"]))
        print(f"  {numbers[problem.key]}: {problem.short_description}")

    print("\nCreating changes...")
    for index, change in enumerate(CHANGES):
        record = client.insert("change_request", _change_payload(client, change, team[index % len(team)]))
        numbers[change.key], sys_ids[change.key] = record.get("number", ""), record["sys_id"]
        client.patch("change_request", record["sys_id"], {"state": "3", "active": "false"}, tolerate=True)
        client.created.append(("change_request", numbers[change.key], record["sys_id"]))
        print(f"  {numbers[change.key]}: {change.short_description}")

    print("\nCreating incidents...")
    for index, incident in enumerate(INCIDENTS):
        payload = _incident_payload(client, incident, team[index % len(team)])
        if incident.problem_key and incident.problem_key in sys_ids:
            payload["problem_id"] = sys_ids[incident.problem_key]
        if incident.change_key and incident.change_key in sys_ids:
            payload["rfc"] = sys_ids[incident.change_key]

        record = client.insert("incident", payload)
        number, sys_id = record.get("number", ""), record["sys_id"]
        numbers[incident.key] = number
        client.created.append(("incident", number, sys_id))

        # Each PATCH of work_notes appends one journal entry, so they go in one at a time.
        for note in incident.work_notes:
            client.patch("incident", sys_id, {"work_notes": note})

        marker = "  TARGET " if incident.is_target else "  "
        state = "open" if incident.open_now else "resolved"
        print(f"{marker}{number}: [{state}] {incident.short_description}")

    return numbers


def _incident_payload(client: SeedClient, incident: Incident, assignee: str) -> dict[str, Any]:
    group_id = client.resolve_or_create("sys_user_group", incident.group)
    payload: dict[str, Any] = {
        "short_description": incident.short_description,
        "description": incident.description,
        "category": incident.category,
        "correlation_id": DEMO_TAG,
        "correlation_display": DEMO_TAG,
        "priority": incident.priority,
        "urgency": incident.priority,
        "impact": incident.priority,
        "assignment_group": group_id,
        "assigned_to": assignee,
        "contact_type": "self-service",
    }
    if incident.ci:
        payload["cmdb_ci"] = client.resolve_or_create("cmdb_ci_appl", incident.ci)
    if incident.service:
        payload["business_service"] = client.resolve_or_create("cmdb_ci_service", incident.service)

    if incident.open_now:
        payload.update(
            state=STATE_IN_PROGRESS,
            opened_at=utc(hours_ago=incident.hours_ago),
        )
        return payload

    opened = datetime.now(timezone.utc) - timedelta(days=incident.days_ago)
    resolved = opened + timedelta(hours=incident.resolve_hours)
    payload.update(
        state=STATE_RESOLVED,
        opened_at=opened.strftime(SN_DATETIME),
        resolved_at=resolved.strftime(SN_DATETIME),
        close_code=incident.close_code,
        close_notes=incident.close_notes,
        reopen_count=str(incident.reopen_count),
        resolved_by=assignee,
    )
    return payload


def _problem_payload(client: SeedClient, problem: Problem, assignee: str) -> dict[str, Any]:
    opened = datetime.now(timezone.utc) - timedelta(days=problem.days_ago)
    payload = {
        "short_description": problem.short_description,
        "description": problem.description,
        "cause_notes": problem.cause_notes,
        "fix_notes": problem.fix_notes,
        "workaround": problem.workaround,
        "category": problem.category,
        "correlation_id": DEMO_TAG,
        "correlation_display": DEMO_TAG,
        "opened_at": opened.strftime(SN_DATETIME),
        "assignment_group": client.resolve_or_create("sys_user_group", problem.group),
        "assigned_to": assignee,
    }
    if problem.ci:
        payload["cmdb_ci"] = client.resolve_or_create("cmdb_ci_appl", problem.ci)
    return payload


def _change_payload(client: SeedClient, change: Change, assignee: str) -> dict[str, Any]:
    opened = datetime.now(timezone.utc) - timedelta(days=change.days_ago)
    payload = {
        "short_description": change.short_description,
        "description": change.description,
        "close_notes": change.close_notes,
        "close_code": "successful",
        "type": "emergency",
        "correlation_id": DEMO_TAG,
        "correlation_display": DEMO_TAG,
        "start_date": opened.strftime(SN_DATETIME),
        "end_date": (opened + timedelta(hours=2)).strftime(SN_DATETIME),
        "assignment_group": client.resolve_or_create("sys_user_group", change.group),
        "assigned_to": assignee,
    }
    if change.ci:
        payload["cmdb_ci"] = client.resolve_or_create("cmdb_ci_appl", change.ci)
    return payload


def cleanup(client: SeedClient, *, confirmed: bool) -> None:
    found: list[tuple[str, str, str]] = []
    for table in ("incident", "problem", "change_request"):
        for row in client.query(table, f"correlation_id={DEMO_TAG}", fields="sys_id,number", limit=500):
            found.append((table, row.get("number", ""), row["sys_id"]))

    if not found:
        print(f"Nothing tagged {DEMO_TAG} to remove.")
        return
    print(f"Found {len(found)} demo records tagged {DEMO_TAG}:")
    for table, number, _ in found:
        print(f"  {table}: {number}")
    if not confirmed:
        print("\nRe-run with --cleanup --yes to delete these. Nothing was deleted.")
        return
    for table, number, sys_id in found:
        client.delete(table, sys_id)
        print(f"  deleted {number}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed demo tickets into a ServiceNow developer instance")
    parser.add_argument("--dry-run", action="store_true", help="show what would be created, write nothing")
    parser.add_argument("--cleanup", action="store_true", help="delete the records this script created")
    parser.add_argument("--yes", action="store_true", help="confirm deletion when used with --cleanup")
    args = parser.parse_args(argv)

    load_dotenv()
    instance = env("SERVICENOW_INSTANCE_URL")
    user = env("SN_SEED_USERNAME", "SERVICENOW_USERNAME")
    password = env("SN_SEED_PASSWORD", "SERVICENOW_PASSWORD")
    if not (instance and user and password):
        print(
            "Set SERVICENOW_INSTANCE_URL and admin credentials first.\n"
            "  In .env:  SN_SEED_USERNAME=admin  and  SN_SEED_PASSWORD=...\n"
            "  (kept separate from the agent's own least-privilege user)"
        )
        return 2

    print(f"Instance: {instance}{'  [dry run]' if args.dry_run else ''}")
    client = SeedClient(instance, user, password, dry_run=args.dry_run)

    try:
        if args.cleanup:
            cleanup(client, confirmed=args.yes)
            return 0
        numbers = seed(client)
    except httpx.HTTPError as exc:
        print(f"\nCould not reach {instance}: {exc}")
        print("Check the instance URL, that the instance is awake, and the credentials.")
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1

    print("\nDone. Tickets to try the agent on:\n")
    for incident in INCIDENTS:
        if not incident.is_target:
            continue
        print(f"  sn-agent resolve {numbers.get(incident.key, '?')} --dry-run")
        print(f"      {incident.expectation}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
