"""DID derivation for agents.

Kept separate from :mod:`core.crypto` so that modules that just need a
DID string (e.g. the registration code path) don't pull in cryptography
imports.

The DID method is ``did:web``: the identifier ``did:web:HOST:agents:ID``
resolves by HTTP fetch of ``https://HOST/agents/ID/did.json``. This is
the simplest W3C DID method that doesn't require a blockchain — it
just relies on the platform serving the right document at the right URL.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

# Last-resort fallback when SERVER_BASE_URL is unset and no host can be
# derived. This is intentionally generic — self-hosters set SERVER_BASE_URL
# in their environment (or the app fails fast at startup), so this default
# only matters in degenerate test fixtures.
_DEFAULT_HOST = "localhost"


def _did_host_from_base_url(server_base_url: str | None) -> str:
    """Extract the ``did:web`` host segment from ``SERVER_BASE_URL``.

    Localhost-with-port becomes ``localhost%3A<port>`` per the did:web
    spec (the colon between host and port must be percent-encoded
    because ``:`` is the DID component separator).
    """
    if not server_base_url:
        return _DEFAULT_HOST
    parsed = urlparse(server_base_url.strip())
    host = parsed.hostname or _DEFAULT_HOST
    port = parsed.port
    if port and port not in (80, 443):
        return f"{host}%3A{port}"
    return host


def build_agent_did(agent_id: str, server_base_url: str | None = None) -> str:
    """Return the agent's ``did:web`` identifier.

    ``server_base_url`` defaults to the ``SERVER_BASE_URL`` env var (the
    same one used elsewhere in the app to construct public links). The
    DID is frozen on the agent row at registration time so it survives
    later hostname changes.
    """
    base = (
        server_base_url
        if server_base_url is not None
        else os.environ.get("SERVER_BASE_URL")
    )
    host = _did_host_from_base_url(base)
    return f"did:web:{host}:agents:{agent_id}"


def did_document_url(agent_id: str, server_base_url: str | None = None) -> str:
    """Return the public URL that resolves the agent's DID document.

    External verifiers fetch this URL when they want to validate a
    signature: it returns the agent's public key in JWK form.
    """
    base = (
        server_base_url
        if server_base_url is not None
        else os.environ.get("SERVER_BASE_URL")
    )
    base = (base or f"http://{_DEFAULT_HOST}:8000").rstrip("/")
    return f"{base}/agents/{agent_id}/did.json"
