"""TLS trust for outbound calls.

Corporate networks re-sign HTTPS with their own CA, which Python's bundled
certifi roots do not know about — both ServiceNow and the GenAI Lab gateway
fail with CERTIFICATE_VERIFY_FAILED on a lab laptop. The CA *is* in the
Windows certificate store, so trusting the OS store keeps verification real
instead of turning it off."""

from __future__ import annotations

import logging
import ssl
from typing import Any

log = logging.getLogger(__name__)


def ssl_context(verify: bool = True) -> Any:
    """`verify=False` disables verification entirely — the handbook's own
    workaround, and the escape hatch for a machine whose store lacks the CA."""

    if not verify:
        return False
    try:
        import truststore
    except ImportError:  # pragma: no cover - truststore is a declared dependency
        log.warning("truststore missing; falling back to the bundled CA roots")
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
