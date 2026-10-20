from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sn_agent.models import JournalEntry, Ticket

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def make_ticket(
    number: str,
    *,
    short_description: str = "VPN client fails to connect",
    description: str = "Users get error 809 when connecting to the corporate VPN from home.",
    close_notes: str = "",
    table: str = "incident",
    category: str = "network",
    cmdb_ci: str = "VPN Gateway",
    business_service: str = "Remote Access",
    assignment_group: str = "Network Support",
    assigned_to: str = "",
    resolved_by: str = "",
    active: bool = False,
    days_ago: int = 30,
    reopen_count: int = 0,
    work_notes: list[str] | None = None,
    state_label: str = "",
) -> Ticket:
    opened = NOW - timedelta(days=days_ago)
    return Ticket(
        sys_id=f"sys-{number}",
        number=number,
        table=table,
        short_description=short_description,
        description=description,
        close_notes=close_notes,
        category=category,
        cmdb_ci=cmdb_ci,
        cmdb_ci_id=f"ci-{cmdb_ci.lower().replace(' ', '-')}" if cmdb_ci else "",
        business_service=business_service,
        assignment_group=assignment_group,
        assigned_to=assigned_to,
        resolved_by=resolved_by,
        active=active,
        state_label=state_label or ("In Progress" if active else "Closed"),
        opened_at=opened,
        resolved_at=None if active else opened + timedelta(hours=6),
        updated_on=opened + timedelta(hours=6),
        reopen_count=reopen_count,
        journal=[
            JournalEntry(element="work_notes", value=note, created_on=opened + timedelta(hours=i + 1))
            for i, note in enumerate(work_notes or [])
        ],
    )
