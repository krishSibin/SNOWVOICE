"""Strip personal data and secrets before any ticket text is sent to the LLM.

IP addresses and hostnames are deliberately kept: they are part of the
troubleshooting value."""

from __future__ import annotations

import re

EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s-]?)?(?:\(\d{2,4}\)[\s-]?)?\d{3,5}[\s-]\d{3,5}(?:[\s-]\d{3,5})?(?![\w.])")
LONG_DIGITS = re.compile(r"\b\d{12,19}\b")
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret|authorization)\b\s*[:=]\s*\S+"
)
BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")
SIGNATURE = re.compile(
    r"(?im)^\s*(sent from my \w+|--\s*$|thanks(?: and regards)?,?\s*$|best regards,?\s*$|regards,?\s*$)"
)


def redact(text: str) -> str:
    if not text:
        return ""
    out = SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}: [REDACTED]", text)
    out = BEARER.sub("Bearer [REDACTED]", out)
    out = EMAIL.sub("[EMAIL]", out)
    out = LONG_DIGITS.sub("[NUMBER]", out)
    out = PHONE.sub("[PHONE]", out)
    out = SIGNATURE.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()
