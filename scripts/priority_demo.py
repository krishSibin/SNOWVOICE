"""Raise a P3 and then a P1 incident, to show the agent works the P1 first.

The P3 goes in first, so it is the older ticket. If the agent answered in
arrival order it would take the P3; it takes the P1 because the sweep and the
work queue both order by priority before age.

    python scripts/priority_demo.py              # P3 badge ticket, then P1 VPN ticket
    python scripts/priority_demo.py --gap 5      # wait 5 seconds between the two
    python scripts/priority_demo.py --dry-run    # print the payloads, create nothing

For the clearest demo, run the agent with one worker so the second ticket
visibly waits for the first:

    WORKER_THREADS=1   (in .env)   then   sn-agent serve -v

Both tickets carry correlation_id = "sn-agent-demo", so
`python scripts/seed_demo_data.py --cleanup --yes` removes them with the rest.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scripts.new_ticket import PRESETS  # noqa: E402
from scripts.seed_demo_data import (  # noqa: E402
    SeedClient,
    _incident_payload,
    env,
    load_dotenv,
)

# ServiceNow derives priority from impact x urgency and overwrites whatever
# priority is sent, so these pairs are what actually decide it.
#   impact 1, urgency 1 -> 1 - Critical
#   impact 2, urgency 2 -> 3 - Moderate
IMPACT_URGENCY = {"1": ("1", "1"), "3": ("2", "2")}

# Created in this order: the P3 first, so it is the older of the two.
PRIORITIES = ("3", "1")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="priority_demo",
        description="Raise a P3 then a P1 incident to demo priority ordering",
    )
    parser.add_argument("--p3", default="badge", help=f"preset for the P3 ticket ({', '.join(sorted(PRESETS))})")
    parser.add_argument("--p1", default="vpn", help=f"preset for the P1 ticket ({', '.join(sorted(PRESETS))})")
    parser.add_argument("--gap", type=float, default=2.0, help="seconds between creating the two tickets (default: 2)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be created, write nothing")
    args = parser.parse_args(argv)

    for name in (args.p3, args.p1):
        if name not in PRESETS:
            print(f"No preset named {name!r}. Known: {', '.join(sorted(PRESETS))}")
            return 2

    load_dotenv()
    instance = env("SERVICENOW_INSTANCE_URL")
    user = env("SN_SEED_USERNAME")
    password = env("SN_SEED_PASSWORD")
    if not (instance and user and password):
        print(
            "Set SERVICENOW_INSTANCE_URL, SN_SEED_USERNAME and SN_SEED_PASSWORD in .env.\n"
            "  (an admin login: the agent's own account cannot create tickets)"
        )
        return 2

    print(f"Instance: {instance}{'  [dry run]' if args.dry_run else ''}")
    client = SeedClient(instance, user, password, dry_run=args.dry_run)
    users = client.pick_users()

    raised: list[tuple[str, str, str]] = []
    for index, priority in enumerate(PRIORITIES):
        preset = args.p3 if priority == "3" else args.p1
        # Opened now, not backdated: the sweep only looks at recent tickets.
        incident = replace(PRESETS[preset], open_now=True, hours_ago=0, priority=priority)
        payload = _incident_payload(client, incident, users, index)
        payload["impact"], payload["urgency"] = IMPACT_URGENCY[priority]

        if args.dry_run:
            print(f"\nWould raise P{priority}: {incident.short_description}")
            for field in ("priority", "impact", "urgency", "state", "opened_at"):
                print(f"  {field}: {payload.get(field)}")
            continue

        record = client.insert("incident", payload)
        number, sys_id = record.get("number", ""), record["sys_id"]
        actual = str(record.get("priority", "?"))
        print(f"\nRaised {number} as P{actual}: {incident.short_description}")
        print(f"  {instance.rstrip('/')}/nav_to.do?uri=incident.do%3Fsys_id%3D{sys_id}")
        if actual != priority:
            print(f"  ! asked for P{priority} but ServiceNow set P{actual} - check the priority lookup rules")
        raised.append((actual, number, incident.short_description))

        if index < len(PRIORITIES) - 1 and args.gap > 0:
            time.sleep(args.gap)

    if args.dry_run:
        return 0

    print("\nOrder created (oldest first):")
    for actual, number, short in raised:
        print(f"  P{actual}  {number:<11} {short}")
    print("\nThe next sweep of `sn-agent serve` should pick up the P1 first, even though the P3 is older.")
    print("Watch for 'picked up (P1)' in the log before 'picked up (P3)'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
