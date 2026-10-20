"""Raise one fresh open incident, so a running `sn-agent serve` can pick it up live.

`serve` works the queue until nothing is left, which is the right behaviour and a
dull demo: once the backlog is clear there is nothing to watch. This drops a
single new ticket into the instance, opened just now, so the next sweep finds it
and answers it while the audience watches.

    python scripts/new_ticket.py             # the VPN ticket: rich history, confident answer
    python scripts/new_ticket.py --list      # what else it can raise
    python scripts/new_ticket.py badge       # no history at all, so the agent says so
    python scripts/new_ticket.py --short "..." --description "..."
    python scripts/new_ticket.py --dry-run   # print the payload, create nothing

Every ticket is tagged correlation_id = "sn-agent-demo", so the usual
`python scripts/seed_demo_data.py --cleanup --yes` removes it with the rest.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scripts.demo_data import INCIDENTS, Incident  # noqa: E402
from scripts.seed_demo_data import (  # noqa: E402
    SeedClient,
    _incident_payload,
    env,
    load_dotenv,
)

# The seeded storylines that end in an open ticket, keyed by a short name to type.
PRESETS: dict[str, Incident] = {
    incident.storyline.split("-", 1)[0]: incident
    for incident in INCIDENTS
    if incident.is_target
}


def _describe(incident: Incident) -> str:
    return incident.expectation or "no stated expectation"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="new_ticket",
        description="Raise one open incident for a live demo of sn-agent serve",
    )
    parser.add_argument(
        "preset", nargs="?", default="vpn",
        help=f"which storyline to raise ({', '.join(sorted(PRESETS))}); default: vpn",
    )
    parser.add_argument("--list", action="store_true", help="show the presets and stop")
    parser.add_argument("--short", default="", help="override the short description")
    parser.add_argument("--description", default="", help="override the description")
    parser.add_argument("--priority", default="", help="override the priority (1-5)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be created, write nothing")
    args = parser.parse_args(argv)

    if args.list:
        print("Presets:\n")
        for name, incident in sorted(PRESETS.items()):
            print(f"  {name:<9} {incident.short_description}")
            print(f"  {'':<9} {_describe(incident)}\n")
        return 0

    if args.preset not in PRESETS:
        print(f"No preset named {args.preset!r}. Known: {', '.join(sorted(PRESETS))}")
        return 2

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
    # The agent's own account may read tickets and add work notes, nothing else,
    # so falling back to it here buys a 403 at the insert. Say so before the call.
    if not args.dry_run and user == env("SERVICENOW_USERNAME"):
        print(
            f"! SN_SEED_USERNAME is not set, so this would authenticate as {user!r} -\n"
            "  the agent's least-privilege account, which cannot create tickets.\n"
            "  Set SN_SEED_USERNAME and SN_SEED_PASSWORD in .env to an admin login."
        )
        return 2

    # Opened now, not backdated: the sweep only looks at recent tickets.
    incident = replace(PRESETS[args.preset], open_now=True, hours_ago=0)
    if args.short:
        incident = replace(incident, short_description=args.short)
    if args.description:
        incident = replace(incident, description=args.description)
    if args.priority:
        incident = replace(incident, priority=args.priority)

    print(f"Instance: {instance}{'  [dry run]' if args.dry_run else ''}")
    client = SeedClient(instance, user, password, dry_run=args.dry_run)
    payload = _incident_payload(client, incident, client.pick_users(), 0)

    if args.dry_run:
        print(f"\nWould raise: {incident.short_description}")
        for field in ("category", "priority", "state", "opened_at", "correlation_id"):
            if field in payload:
                print(f"  {field}: {payload[field]}")
        return 0

    record = client.insert("incident", payload)
    number, sys_id = record.get("number", ""), record["sys_id"]
    print(f"\nRaised {number}: {incident.short_description}")
    print(f"  {instance.rstrip('/')}/nav_to.do?uri=incident.do%3Fsys_id%3D{sys_id}")
    print(f"  expected: {_describe(incident)}")
    print("\nA running `sn-agent serve` picks this up on its next sweep.")
    print(f"To answer it directly instead:  sn-agent resolve {number} -v")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
