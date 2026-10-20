"""Print a sample work note without touching ServiceNow or the TCS GenAI Lab gateway.

Uses the in-memory index and the scripted LLM from the tests, so you can see the
note format and the confidence breakdown before any credentials exist:

    python scripts/demo_note.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from sn_agent.notify.formatting import render_note  # noqa: E402
from tests.factories import NOW, make_ticket  # noqa: E402
from tests.test_pipeline import build  # noqa: E402


def main() -> None:
    history = [
        make_ticket(
            "INC0012345",
            close_notes=(
                "Certificate on the VPN gateway had expired. Re-issued it from the internal CA, "
                "restarted the ike service, and users reconnected."
            ),
            days_ago=40,
        ),
        make_ticket(
            "INC0012901",
            close_notes="Re-issued the expired VPN gateway certificate.",
            days_ago=120,
        ),
        make_ticket("INC0011002", close_notes="Fixed", resolved_by="Roja M", days_ago=200),
    ]

    duplicate = make_ticket(
        "INC0013500",
        active=True,
        days_ago=0,
        assigned_to="Geethu V",
        work_notes=["Restarted the VPN gateway service, users still report error 809."],
    )
    duplicate.opened_at = NOW - timedelta(hours=3)

    pipeline, _llm, _settings = build(
        history,
        grades={
            "INC0012345": ("relevant", 0.92, "unrelated"),
            "INC0012901": ("relevant", 0.86, "unrelated"),
            "INC0011002": ("relevant", 0.80, "unrelated"),
            "INC0013500": ("relevant", 0.90, "duplicate"),
        },
        open_tickets=[duplicate],
    )

    result = pipeline.run(make_ticket("INC0013777", active=True, days_ago=0))
    print(render_note(result, instance_url="https://acme.service-now.com"))


if __name__ == "__main__":
    main()
