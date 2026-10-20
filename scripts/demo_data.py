"""Demo ticket data for a fresh ServiceNow developer instance.

Each storyline is built to exercise one behaviour of the agent, so a seeded
instance can demonstrate all four outcomes plus the duplicate and major-incident
paths. `scripts/seed_demo_data.py` is what pushes these into an instance.
"""

from __future__ import annotations

from dataclasses import dataclass

DEMO_TAG = "sn-agent-demo"


@dataclass
class Incident:
    key: str
    storyline: str
    short_description: str
    description: str
    category: str = "software"
    ci: str = ""
    service: str = ""
    group: str = "Service Desk"
    days_ago: int = 30
    resolve_hours: int = 6
    close_notes: str = ""
    close_code: str = "Solution provided"
    work_notes: tuple[str, ...] = ()
    reopen_count: int = 0
    priority: str = "3"
    open_now: bool = False
    hours_ago: int = 0  # for open tickets: how long ago it was raised
    problem_key: str = ""
    change_key: str = ""
    is_target: bool = False
    expectation: str = ""


@dataclass
class Problem:
    key: str
    short_description: str
    description: str
    cause_notes: str = ""
    fix_notes: str = ""
    workaround: str = ""
    category: str = "software"
    ci: str = ""
    group: str = "Service Desk"
    days_ago: int = 60


@dataclass
class Change:
    key: str
    short_description: str
    description: str
    close_notes: str = ""
    category: str = "Network"
    ci: str = ""
    group: str = "Network"
    days_ago: int = 45


CIS = [
    "VPN Gateway",
    "Payroll Export Service",
    "Exchange Mailbox Server",
    "SAP Production",
    "Badge Access Controller",
    "Finance Reporting Portal",
]

GROUPS = ["Network", "Service Desk", "Software", "Database", "Hardware"]

# The support team every demo ticket is assigned to, in rotation. They are added
# to each group above, and the manager manages both the people and the groups.
TEAM = ["VishnuPrasad M", "Siddamma Moole", "Roja M", "Geethu V"]
MANAGER = "Anoop A K"


# --- storyline 1: documented fix, should produce a confident answer ----------

VPN = [
    Incident(
        key="vpn_past_1",
        storyline="vpn-cert",
        short_description="Remote users cannot connect to VPN, error 809",
        description=(
            "Multiple users working from home report that the VPN client fails immediately with "
            "error 809. Office users on the LAN are unaffected. Started this morning."
        ),
        category="network",
        ci="VPN Gateway",
        service="Remote Access",
        group="Network",
        days_ago=40,
        close_notes=(
            "The IKE certificate on the VPN gateway had expired overnight. Re-issued the certificate "
            "from the internal CA, imported it on both gateway nodes, and restarted the ike service. "
            "Users reconnected without changing anything on the client. Added a renewal reminder 30 "
            "days before expiry."
        ),
        work_notes=(
            "Reproduced with the test account. Client log shows the tunnel dropping during phase 1.",
            "Gateway log: certificate verify failed, notAfter date is yesterday. Raising with the PKI team.",
        ),
        problem_key="vpn_problem",
        change_key="vpn_change",
    ),
    Incident(
        key="vpn_past_2",
        storyline="vpn-cert",
        short_description="VPN connection fails with error 809 for home users",
        description=(
            "Users on the remote access VPN get error 809 when connecting. Same symptoms as the "
            "outage earlier in the year."
        ),
        category="network",
        ci="VPN Gateway",
        service="Remote Access",
        group="Network",
        days_ago=120,
        close_notes=(
            "Same cause as before: expired gateway certificate. Re-issued it from the internal CA and "
            "restarted the ike service on both nodes. Confirmed with three affected users."
        ),
        work_notes=("Checked certificate expiry first this time, which saved about an hour.",),
    ),
    Incident(
        key="vpn_distractor",
        storyline="vpn-cert",
        short_description="VPN is very slow for remote users on large file transfers",
        description=(
            "Users can connect to the VPN but transfers of large files stall. No error message, the "
            "session stays up."
        ),
        category="network",
        ci="VPN Gateway",
        service="Remote Access",
        group="Network",
        days_ago=75,
        close_notes=(
            "Unrelated to authentication. MTU on the tunnel interface was fragmenting large packets. "
            "Lowered the MTU to 1400 on the gateway and throughput returned to normal."
        ),
    ),
    Incident(
        key="vpn_target",
        storyline="vpn-cert",
        short_description="Cannot connect to VPN from home, error 809",
        description=(
            "Since this morning I get error 809 as soon as I try to connect to the company VPN. "
            "Restarted the laptop and reinstalled the client, no change. Two colleagues on my team "
            "have the same problem."
        ),
        category="network",
        ci="VPN Gateway",
        service="Remote Access",
        group="Network",
        open_now=True,
        hours_ago=2,
        is_target=True,
        expectation="RESOLUTION_FOUND: should cite the two past certificate incidents with high confidence.",
    ),
]

VPN_PROBLEM = Problem(
    key="vpn_problem",
    short_description="VPN gateway certificates expire without warning",
    description="Certificate expiry on the VPN gateway has caused two full remote-access outages.",
    cause_notes="No monitoring on the IKE certificate expiry date, and renewal was a manual task nobody owned.",
    fix_notes=(
        "Added a certificate expiry check to monitoring with a 30-day warning, and documented the "
        "renewal steps: issue from the internal CA, import on both nodes, restart the ike service."
    ),
    workaround="Re-issue the certificate and restart the ike service on both gateway nodes.",
    category="network",
    ci="VPN Gateway",
    group="Network",
    days_ago=38,
)

VPN_CHANGE = Change(
    key="vpn_change",
    short_description="Renew VPN gateway IKE certificate",
    description="Emergency change to re-issue and install the expired IKE certificate on both VPN gateway nodes.",
    close_notes=(
        "Certificate re-issued from the internal CA and installed on both nodes. ike service restarted "
        "one node at a time to avoid a second outage. Remote access confirmed working."
    ),
    ci="VPN Gateway",
    group="Network",
    days_ago=40,
)


# --- storyline 2: same issue before, nobody wrote down the fix --------------

PAYROLL = [
    Incident(
        key="payroll_past_1",
        storyline="payroll-undocumented",
        short_description="Payroll export job fails with ORA-01652",
        description="The nightly payroll export to the bank failed. Job log ends with ORA-01652: unable to extend temp segment.",
        category="database",
        ci="Payroll Export Service",
        service="Payroll",
        group="Database",
        days_ago=200,
        close_notes="Fixed",
    ),
    Incident(
        key="payroll_past_2",
        storyline="payroll-undocumented",
        short_description="Payroll export failed overnight, ORA-01652 in the job log",
        description="Payroll export did not produce the bank file. Same ORA-01652 error as last time.",
        category="database",
        ci="Payroll Export Service",
        service="Payroll",
        group="Database",
        days_ago=140,
        # ServiceNow's data policy forces resolution notes to be non-empty, so the
        # real-world "nothing recorded" case looks like this rather than blank.
        close_notes="n/a",
        close_code="No resolution provided",
    ),
    Incident(
        key="payroll_past_3",
        storyline="payroll-undocumented",
        short_description="Payroll bank file missing, export job error ORA-01652",
        description="Finance reports the bank file was not delivered. The export job shows ORA-01652 again.",
        category="database",
        ci="Payroll Export Service",
        service="Payroll",
        group="Database",
        days_ago=65,
        close_notes="resolved - user confirmed",
        work_notes=("Ran the job again and it completed.",),
    ),
    Incident(
        key="payroll_target",
        storyline="payroll-undocumented",
        short_description="Payroll export job failed with ORA-01652 again",
        description=(
            "Tonight's payroll export did not produce the bank file. The job log ends with "
            "ORA-01652: unable to extend temp segment by 128 in tablespace TEMP. Finance needs the "
            "file before 14:00."
        ),
        category="database",
        ci="Payroll Export Service",
        service="Payroll",
        group="Database",
        open_now=True,
        hours_ago=3,
        priority="2",
        is_target=True,
        expectation=(
            "SIMILAR_NO_RESOLUTION_RECORDED: three past tickets, none with a usable fix. Should name "
            "who closed them instead of inventing steps."
        ),
    ),
]


# --- storyline 3: live duplicates and a major incident ----------------------

EXCHANGE = [
    Incident(
        key="exchange_past",
        storyline="exchange-outage",
        short_description="Outlook stuck on Trying to connect for several users",
        description="Users in the Dublin office cannot send or receive mail. Outlook shows 'Trying to connect'.",
        category="software",
        ci="Exchange Mailbox Server",
        service="Email",
        group="Software",
        days_ago=300,
        close_notes=(
            "The transport service on MBX02 had stopped after a failed update. Restarted the transport "
            "service and cleared the stuck queue of about 4000 messages. Mail flow recovered in ten minutes."
        ),
    ),
    Incident(
        key="exchange_open_1",
        storyline="exchange-outage",
        short_description="Cannot send email, Outlook says Trying to connect",
        description="Outlook has been stuck on 'Trying to connect' since about 09:00. Webmail also times out.",
        category="software",
        ci="Exchange Mailbox Server",
        service="Email",
        group="Software",
        open_now=True,
        hours_ago=4,
        priority="2",
        work_notes=(
            "Restarted the transport service on MBX01, no change. Queue is growing on MBX02 as well.",
        ),
    ),
    Incident(
        key="exchange_open_2",
        storyline="exchange-outage",
        short_description="Email not working - Outlook disconnected",
        description="Whole finance team cannot send mail. Outlook shows disconnected in the status bar.",
        category="software",
        ci="Exchange Mailbox Server",
        service="Email",
        group="Software",
        open_now=True,
        hours_ago=3,
        priority="2",
    ),
    Incident(
        key="exchange_target",
        storyline="exchange-outage",
        short_description="Outlook stuck at Trying to connect, cannot send mail",
        description=(
            "Nobody on my floor can send email. Outlook sits on 'Trying to connect' and eventually "
            "times out. Started around 09:00 this morning."
        ),
        category="software",
        ci="Exchange Mailbox Server",
        service="Email",
        group="Software",
        open_now=True,
        hours_ago=1,
        priority="2",
        is_target=True,
        expectation=(
            "ACTIVE duplicates + major incident warning: two other open tickets in the last 24h, with "
            "what has already been tried, plus the old transport-service fix as a suggestion."
        ),
    ),
]


# --- storyline 4: nothing similar has ever happened ------------------------

BADGE = [
    Incident(
        key="badge_target",
        storyline="badge-reader",
        short_description="New RFID badge reader in Lab 3 rejects all cards",
        description=(
            "The badge reader installed yesterday outside Lab 3 flashes red for every card, including "
            "newly issued ones. The reader next to it on the same door works."
        ),
        category="hardware",
        ci="Badge Access Controller",
        service="Facilities",
        group="Hardware",
        open_now=True,
        hours_ago=5,
        is_target=True,
        expectation="NO_HISTORY: nothing comparable in the history, so it should say so and ask diagnostic questions.",
    ),
]


# --- storyline 5: similar words, different problem (should not be confident) -

SAP = [
    Incident(
        key="sap_distractor_1",
        storyline="sap-trap",
        short_description="SAP GUI crashes on login for Windows 11 users",
        description="SAP GUI closes immediately after entering credentials on Windows 11 laptops.",
        category="software",
        ci="SAP Production",
        service="ERP",
        group="Software",
        days_ago=90,
        close_notes=(
            "SAP GUI 7.60 is not compatible with the new Windows 11 build. Upgraded affected clients "
            "to SAP GUI 8.00 patch 12 and the crash stopped."
        ),
    ),
    Incident(
        key="sap_distractor_2",
        storyline="sap-trap",
        short_description="SAP nightly batch job slow but completes",
        description="The nightly batch window is running about 40 minutes longer than usual.",
        category="software",
        ci="SAP Production",
        service="ERP",
        group="Database",
        days_ago=55,
        close_notes="Cleared old spool files and the batch window returned to normal.",
        reopen_count=2,
    ),
    Incident(
        key="sap_target",
        storyline="sap-trap",
        short_description="SAP nightly batch job fails with RFC timeout to external system",
        description=(
            "Job Z_INVOICE_SYNC fails around 02:30 with 'RFC timeout' when calling the external "
            "invoicing partner. No changes on our side that we know of."
        ),
        category="software",
        ci="SAP Production",
        service="ERP",
        group="Software",
        open_now=True,
        hours_ago=6,
        is_target=True,
        expectation=(
            "INSUFFICIENT_EVIDENCE: the history shares product names but not the problem, so it should "
            "refuse to give steps rather than stretch the GUI-crash fix to fit."
        ),
    ),
]


# --- background noise, so retrieval has to discriminate --------------------

NOISE = [
    Incident(
        key="noise_1", storyline="noise",
        short_description="Printer on 3rd floor jams on duplex printing",
        description="The shared printer jams every time duplex printing is used.",
        category="hardware", group="Hardware", days_ago=25,
        close_notes="Worn duplex roller. Replaced the roller assembly under the maintenance contract.",
    ),
    Incident(
        key="noise_2", storyline="noise",
        short_description="Account locked out after password change",
        description="User cannot log in after changing the password, account shows as locked.",
        category="software", group="Service Desk", days_ago=18, close_notes="Unlocked the account.",
    ),
    Incident(
        key="noise_3", storyline="noise",
        short_description="Laptop battery drains in under two hours",
        description="Battery life dropped sharply on a two-year-old laptop.",
        category="hardware", group="Hardware", days_ago=48,
        close_notes="Battery health at 61 percent. Replaced the battery and advised on power settings.",
    ),
    Incident(
        key="noise_4", storyline="noise",
        short_description="Wi-Fi drops in the north meeting rooms",
        description="Clients disconnect from Wi-Fi during meetings in the north wing.",
        category="network", group="Network", days_ago=60,
        close_notes=(
            "Two access points were on the same channel and interfering. Re-planned channels for the "
            "north wing and disconnects stopped."
        ),
    ),
    Incident(
        key="noise_5", storyline="noise",
        short_description="Finance reporting portal shows blank dashboard",
        description="The reporting portal loads but the dashboard area stays empty.",
        category="software", ci="Finance Reporting Portal", service="Finance", group="Software",
        days_ago=35, close_notes="Cache cleared",
    ),
    Incident(
        key="noise_6", storyline="noise",
        short_description="Shared drive S: not mapped after restart",
        description="The S: drive is missing after restarting the laptop.",
        category="software", group="Service Desk", days_ago=12,
        close_notes="Logon script had failed because the laptop was on VPN before login. Mapped the drive manually and documented the workaround.",
    ),
    Incident(
        key="noise_7", storyline="noise",
        short_description="Teams audio cuts out during calls",
        description="Audio drops for a few seconds at a time during Teams calls.",
        category="software", group="Service Desk", days_ago=70, close_notes="done",
    ),
    Incident(
        key="noise_8", storyline="noise",
        short_description="Badge printer produces faded cards",
        description="Newly printed access cards are too faint to scan.",
        category="hardware", ci="Badge Access Controller", service="Facilities", group="Hardware",
        days_ago=95, close_notes="Replaced the print ribbon and recalibrated the badge printer.",
    ),
]


INCIDENTS: list[Incident] = [*VPN, *PAYROLL, *EXCHANGE, *BADGE, *SAP, *NOISE]
PROBLEMS: list[Problem] = [VPN_PROBLEM]
CHANGES: list[Change] = [VPN_CHANGE]


def targets() -> list[Incident]:
    return [i for i in INCIDENTS if i.is_target]
