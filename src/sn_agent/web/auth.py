"""Logins for the web app: two roles, kept in the local database.

- `user`: an employee. Sees only the chatbot and their own conversations.
- `ops`:  the support team. Sees the ops console and can also use the chatbot.

Passwords are stored as scrypt hashes; the login cookie is signed with
APP_SECRET_KEY and expires after 12 hours. Only the standard library is used.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

ROLES = ("user", "ops")
COOKIE = "sn_agent_session"
SESSION_SECONDS = 12 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt_b64), **_SCRYPT)
    return hmac.compare_digest(digest, base64.b64decode(digest_b64))


@dataclass
class Identity:
    email: str
    name: str
    role: str

    @property
    def is_ops(self) -> bool:
        return self.role == "ops"


class Signer:
    def __init__(self, secret: str):
        if not secret:
            # Works, but every restart signs everyone out. Say so once.
            log.warning("APP_SECRET_KEY is not set; logins will not survive a restart")
            secret = secrets.token_hex(32)
        self._key = secret.encode("utf-8")

    def issue(self, identity: Identity) -> str:
        body = json.dumps(
            {"e": identity.email, "n": identity.name, "r": identity.role, "x": int(time.time()) + SESSION_SECONDS},
            separators=(",", ":"),
        ).encode()
        payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
        return f"{payload}.{self._sign(payload)}"

    def read(self, token: str | None) -> Identity | None:
        if not token or "." not in token:
            return None
        payload, signature = token.rsplit(".", 1)
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if data.get("x", 0) < time.time() or data.get("r") not in ROLES:
            return None
        return Identity(email=data["e"], name=data.get("n", ""), role=data["r"])

    def _sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
