"""Create the agent's own restricted ServiceNow account, and verify it.

The agent should never run as admin. This creates `ai.resolution.agent` with:

- the `itil` role, for reading incidents, problems, changes and their notes;
- API-only access (it cannot log in to the web interface);
- a guard rule, enforced by ServiceNow itself, that lets this account add work
  notes and blocks every other change: no field edits, no new records, no
  deletes. A stolen password can do no more than add notes.

It writes the new credentials into .env (never printing the password), then logs
in as the new account and proves each rule holds.

    python scripts/create_agent_user.py           # the ticket agent's account
    python scripts/create_agent_user.py --intake  # the intake chatbot's account

`--intake` creates `ai.intake.agent` instead: the chatbot's account, which may
also create incidents. Its guard rule allows new incidents and work notes, and
blocks every other change, so it still cannot reassign, resolve or delete.
"""

from __future__ import annotations

import re
import secrets
import string
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from sn_agent.tls import ssl_context  # noqa: E402

from scripts.demo_data import DEMO_TAG  # noqa: E402
from scripts.seed_demo_data import SeedClient, env, load_dotenv  # noqa: E402

AGENT_USER = "ai.resolution.agent"
GUARD_RULE_NAME = "AI agent: work notes only"

GUARD_RULE_SCRIPT = r"""(function executeRule(current, previous) {
    // The AI agent account may add work notes to a task. Nothing else.
    var allowed = {work_notes: true, sys_updated_on: true, sys_updated_by: true, sys_mod_count: true};
    if (current.operation() != 'update') {
        gs.addErrorMessage('The AI agent account may only add work notes.');
        current.setAbortAction(true);
        return;
    }
    var fields = current.getElements();
    for (var i = 0; i < fields.size(); i++) {
        var field = fields.get(i);
        if (field.changes() && !allowed[field.getName()]) {
            gs.addErrorMessage('The AI agent account may only add work notes (tried to change ' + field.getName() + ').');
            current.setAbortAction(true);
            return;
        }
    }
})(current, previous);"""


INTAKE_USER = "ai.intake.agent"
INTAKE_GUARD_RULE_NAME = "AI intake agent: new incidents and work notes only"

INTAKE_GUARD_RULE_SCRIPT = r"""(function executeRule(current, previous) {
    // The AI intake account may raise incidents and add work notes. Nothing else.
    if (current.operation() == 'insert') {
        if (current.getTableName() != 'incident') {
            gs.addErrorMessage('The AI intake account may only create incidents.');
            current.setAbortAction(true);
        }
        return;
    }
    var allowed = {work_notes: true, sys_updated_on: true, sys_updated_by: true, sys_mod_count: true};
    if (current.operation() != 'update') {
        gs.addErrorMessage('The AI intake account may only create incidents and add work notes.');
        current.setAbortAction(true);
        return;
    }
    var fields = current.getElements();
    for (var i = 0; i < fields.size(); i++) {
        var field = fields.get(i);
        if (field.changes() && !allowed[field.getName()]) {
            gs.addErrorMessage('The AI intake account may only add work notes (tried to change ' + field.getName() + ').');
            current.setAbortAction(true);
            return;
        }
    }
})(current, previous);"""


def generate_password(length: int = 24) -> str:
    """Meets typical ServiceNow policies: upper, lower, digit and symbol."""

    symbols = "!#%*+-=?@^_"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in symbols for c in password)
        ):
            return password


def ensure_user(admin: SeedClient, password: str, user: str = AGENT_USER, last_name: str = "Resolution Agent") -> str:
    rows = admin.query("sys_user", f"user_name={user}", fields="sys_id")
    fields = {
        "user_name": user,
        "first_name": "AI",
        "last_name": last_name,
        "active": "true",
        "web_service_access_only": "true",
        # Journal timestamps come back in the reading account's timezone.
        "time_zone": "GMT",
    }
    if rows:
        sys_id = rows[0]["sys_id"]
        admin.patch("sys_user", sys_id, fields)
        print(f"  user {user} already existed; password reset")
    else:
        sys_id = admin.insert("sys_user", fields)["sys_id"]
        print(f"  created user {user}")

    # Password fields must be written through the display-value path so
    # ServiceNow hashes them.
    response = admin.http.patch(
        f"/api/now/table/sys_user/{sys_id}",
        params={"sysparm_input_display_value": "true"},
        json={"user_password": password},
    )
    response.raise_for_status()
    return sys_id


def ensure_role(admin: SeedClient, user_sys_id: str, role: str = "itil") -> None:
    role_rows = admin.query("sys_user_role", f"name={role}", fields="sys_id")
    if not role_rows:
        raise RuntimeError(f"role {role} not found on this instance")
    role_id = role_rows[0]["sys_id"]
    if admin.query("sys_user_has_role", f"user={user_sys_id}^role={role_id}", fields="sys_id"):
        print(f"  role {role} already granted")
        return
    admin.insert("sys_user_has_role", {"user": user_sys_id, "role": role_id})
    print(f"  granted role {role}")


def ensure_guard_rule(
    admin: SeedClient,
    *,
    user: str = AGENT_USER,
    name: str = GUARD_RULE_NAME,
    script: str = GUARD_RULE_SCRIPT,
    description: str = "Restricts the AI resolution agent's account to adding work notes.",
) -> None:
    fields = {
        "name": name,
        "collection": "task",  # covers incident, problem and change: they all extend task
        "when": "before",
        "order": "1",  # first, so it sees exactly what the caller asked to change
        "action_insert": "true",
        "action_update": "true",
        "action_delete": "true",
        "active": "true",
        "advanced": "true",
        "condition": f"gs.getUserName() == '{user}'",
        "script": script,
        "description": description,
    }
    rows = admin.query("sys_script", f"name={name}^collection=task", fields="sys_id")
    if rows:
        admin.patch("sys_script", rows[0]["sys_id"], fields)
        print("  guard rule already existed; updated")
    else:
        admin.insert("sys_script", fields)
        print("  created guard rule")


def write_env(password: str) -> None:
    path = ROOT / ".env"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^SERVICENOW_AUTH_MODE=.*$", "SERVICENOW_AUTH_MODE=basic", text)
    text = re.sub(r"(?m)^SERVICENOW_USERNAME=.*$", f"SERVICENOW_USERNAME={AGENT_USER}", text)
    text = re.sub(r"(?m)^SERVICENOW_PASSWORD=.*$", lambda _m: f"SERVICENOW_PASSWORD={password}", text)
    text = text.replace(
        "# TEMPORARY: admin, so the read-only sync can run. Replace with the restricted\n"
        "# agent user (read + work_notes only) before the agent posts to any ticket.\n",
        "# The agent's own restricted account: reads tickets, adds work notes, nothing else.\n",
    )
    path.write_text(text, encoding="utf-8")
    print("  wrote the new credentials to .env (password not shown)")


def write_intake_env(password: str) -> None:
    path = ROOT / ".env"
    text = path.read_text(encoding="utf-8")
    for key, value in (("SN_INTAKE_USERNAME", INTAKE_USER), ("SN_INTAKE_PASSWORD", password)):
        if re.search(rf"(?m)^{key}=", text):
            text = re.sub(rf"(?m)^{key}=.*$", lambda _m: f"{key}={value}", text)
        else:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
    path.write_text(text, encoding="utf-8")
    print("  wrote the intake account's credentials to .env (password not shown)")


def verify_intake(instance: str, admin: SeedClient, password: str) -> bool:
    agent = httpx.Client(
        base_url=instance,
        auth=(INTAKE_USER, password),
        timeout=60.0,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        verify=ssl_context(),
    )
    checks: list[tuple[str, bool]] = []

    def expect(label: str, response: httpx.Response, allowed: bool) -> None:
        ok = (response.status_code < 400) == allowed
        verb = "allowed" if response.status_code < 400 else f"blocked ({response.status_code})"
        checks.append((label, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {verb}")

    created = agent.post(
        "/api/now/table/incident",
        json={"short_description": "Intake permission check - safe to delete", "correlation_id": DEMO_TAG},
    )
    expect("create an incident", created, allowed=True)
    sys_id = created.json().get("result", {}).get("sys_id", "") if created.status_code < 400 else ""
    try:
        expect(
            "list assignment groups",
            agent.get("/api/now/table/sys_user_group", params={"sysparm_limit": "1"}),
            allowed=True,
        )
        expect(
            "look up a caller",
            agent.get("/api/now/table/sys_user", params={"sysparm_limit": "1", "sysparm_fields": "sys_id"}),
            allowed=True,
        )
        if sys_id:
            path = f"/api/now/table/incident/{sys_id}"
            expect("add a work note", agent.patch(path, json={"work_notes": "Permission check."}), allowed=True)
            expect("change the state", agent.patch(path, json={"state": "6"}), allowed=False)
            expect("reassign the ticket", agent.patch(path, json={"assignment_group": ""}), allowed=False)
            expect("delete a ticket", agent.delete(path), allowed=False)
        expect("create a problem", agent.post("/api/now/table/problem", json={"short_description": "should not exist"}), allowed=False)
    finally:
        if sys_id:
            admin.delete("incident", sys_id)
        for leftover in admin.query("problem", "short_description=should not exist", fields="sys_id", limit=5):
            admin.delete("problem", leftover["sys_id"])

    return all(ok for _label, ok in checks)


def verify(instance: str, admin: SeedClient, password: str) -> bool:
    agent = httpx.Client(
        base_url=instance,
        auth=(AGENT_USER, password),
        timeout=60.0,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        verify=ssl_context(),
    )
    checks: list[tuple[str, bool]] = []

    def expect(label: str, response: httpx.Response, allowed: bool) -> None:
        ok = (response.status_code < 400) == allowed
        verb = "allowed" if response.status_code < 400 else f"blocked ({response.status_code})"
        checks.append((label, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {verb}")

    def expect_visible(label: str, response: httpx.Response, text: str) -> None:
        """ServiceNow hides rows it won't show rather than refusing, so a success
        status proves nothing. Check the content actually came back."""

        body = response.text if response.status_code < 400 else ""
        ok = text in body
        checks.append((label, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {'visible' if ok else 'NOT visible'}")

    # A throwaway ticket, so the checks never touch a real one.
    group = admin.resolve_or_create("sys_user_group", "Service Desk")
    members = admin.group_members(group, admin.pick_users())
    test = admin.insert(
        "incident",
        {
            "short_description": "Agent permission check - safe to delete",
            "correlation_id": DEMO_TAG,
            "assignment_group": group,
            "assigned_to": members[0] if members else "",
        },
    )
    path = f"/api/now/table/incident/{test['sys_id']}"
    try:
        expect("read a ticket", agent.get(path), allowed=True)
        expect(
            "search tickets",
            agent.get("/api/now/table/incident", params={"sysparm_query": "123TEXTQUERY321=vpn", "sysparm_limit": "1"}),
            allowed=True,
        )
        marker = "Permission check from the setup script."
        expect("add a work note", agent.patch(path, json={"work_notes": marker}), allowed=True)
        expect_visible(
            "read work notes back",
            agent.get(path, params={"sysparm_fields": "work_notes", "sysparm_display_value": "true"}),
            marker,
        )
        expect("change the state", agent.patch(path, json={"state": "6"}), allowed=False)
        expect("reassign the ticket", agent.patch(path, json={"assignment_group": ""}), allowed=False)
        expect("edit the description", agent.patch(path, json={"short_description": "changed"}), allowed=False)
        expect("create a ticket", agent.post("/api/now/table/incident", json={"short_description": "should not exist"}), allowed=False)
        expect("delete a ticket", agent.delete(path), allowed=False)
        expect(
            "change system settings",
            agent.post("/api/now/table/sys_properties", json={"name": "x_ai_agent.probe", "value": "1"}),
            allowed=False,
        )
    finally:
        admin.delete("incident", test["sys_id"])
        for leftover in admin.query("incident", "short_description=should not exist", fields="sys_id", limit=5):
            admin.delete("incident", leftover["sys_id"])

    return all(ok for _label, ok in checks)


def main() -> int:
    load_dotenv()
    instance = env("SERVICENOW_INSTANCE_URL").rstrip("/")
    admin_user = env("SN_SEED_USERNAME")
    admin_password = env("SN_SEED_PASSWORD")
    if not (instance and admin_user and admin_password):
        print("Needs SERVICENOW_INSTANCE_URL, SN_SEED_USERNAME and SN_SEED_PASSWORD (an admin) in .env")
        return 2

    admin = SeedClient(instance, admin_user, admin_password)
    password = generate_password()

    if "--intake" in sys.argv[1:]:
        print(f"Setting up {INTAKE_USER} on {instance}")
        user_id = ensure_user(admin, password, INTAKE_USER, "Intake Agent")
        ensure_role(admin, user_id)
        ensure_guard_rule(
            admin,
            user=INTAKE_USER,
            name=INTAKE_GUARD_RULE_NAME,
            script=INTAKE_GUARD_RULE_SCRIPT,
            description="Restricts the AI intake agent's account to creating incidents and adding work notes.",
        )
        write_intake_env(password)
        print("\nVerifying as the new account:")
        ok = verify_intake(instance, admin, password)
        print("\nAll checks passed." if ok else "\nSome checks FAILED - do not rely on this account yet.")
        return 0 if ok else 1

    print(f"Setting up {AGENT_USER} on {instance}")
    user_id = ensure_user(admin, password)
    ensure_role(admin, user_id)
    ensure_guard_rule(admin)
    write_env(password)

    print("\nVerifying as the new account:")
    ok = verify(instance, admin, password)
    print("\nAll checks passed." if ok else "\nSome checks FAILED - do not rely on this account yet.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
