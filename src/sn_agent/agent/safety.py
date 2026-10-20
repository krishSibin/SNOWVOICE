"""Flag suggested steps a human should look at twice before following.

Verification proves a step matches what a past ticket says. It cannot prove the
past ticket was right: a harmful "fix" written into old close notes would pass,
because the note really does say it. The agent never runs anything itself, so
the defence is to make risky steps impossible to miss in the note.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')\]]+")

# Inline code, or a command name. Only words that are unambiguous as commands:
# "service", "kill" or "restart" appear in ordinary steps ("restart the ike
# service"), and a warning on every step would soon be ignored.
INLINE_CODE = re.compile(r"`[^`]+`")
COMMAND_WORDS = re.compile(
    r"(?ix)(?:^|[\s;&|(])(?:"
    r"sudo|ssh|scp|curl|wget|chmod|chown|systemctl|pkill|taskkill|"
    r"powershell|pwsh|cmd\.exe|regedit|netsh|net\s+(?:user|stop|start|localgroup)|sc\s+(?:stop|start|config|delete)|"
    r"Invoke-\w+|Set-\w+|Remove-\w+|Stop-\w+|Start-\w+|New-\w+|"
    r"iptables|firewall-cmd|psql|sqlplus|bash"
    r")\b"
)

# Anything that destroys data or opens the system up. Flagged more loudly.
DESTRUCTIVE = re.compile(
    r"(?ix)("
    r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r|"
    r"\bdel\s+/[fsq]|\brmdir\s+/s|\bformat\s+[a-z]:|\bmkfs\b|\bdd\s+if=|"
    r"\bRemove-Item\b.*-Recurse|"
    r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b|\bTRUNCATE\s+TABLE\b|\bDELETE\s+FROM\b|"
    r"\breg\s+delete\b|\bnet\s+user\s+\S+\s+/delete\b|\buserdel\b|"
    r"\bchmod\s+(?:-R\s+)?777\b|\bshutdown\b|\bdisable\s+(?:the\s+)?(?:firewall|antivirus|defender)|"
    r"\|\s*(?:ba)?sh\b|-encodedcommand\b|\s-enc\s"
    r")"
)

FLAG_LABELS = {
    "destructive": "can delete data or weaken security",
    "command": "contains a command",
    "link": "contains a link",
}


def flag_text(text: str, *, trusted_hosts: tuple[str, ...] = ()) -> list[str]:
    """Returns the flags for one suggested step, most serious first."""

    flags: list[str] = []
    if DESTRUCTIVE.search(text):
        flags.append("destructive")
    if INLINE_CODE.search(text) or COMMAND_WORDS.search(text):
        flags.append("command")
    if any(not _trusted(url, trusted_hosts) for url in URL.findall(text)):
        flags.append("link")
    return flags


def _trusted(url: str, trusted_hosts: tuple[str, ...]) -> bool:
    """Links back to our own ServiceNow instance are how citations work, not a
    risk."""

    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    return any(host == h or host.endswith("." + h) for h in trusted_hosts if h)
