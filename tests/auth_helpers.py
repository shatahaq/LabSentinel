"""Shared test doubles for the transport's Authenticator.

``SharedTokenAuth`` accepts any agent presenting one known token. It keeps the
transport and agent-core tests focused on their own behaviour; pairing itself
(short code, rate limiting, per-agent tokens) is covered in test_pairing.py.
"""

from __future__ import annotations

from labsentinel_dashboard.transport.server import AuthResult
from labsentinel_shared.protocol import HelloPayload
from labsentinel_shared.security import hash_token, verify_token


class SharedTokenAuth:
    def __init__(self, token: str) -> None:
        self._hash = hash_token(token)

    def authenticate_hello(
        self, hello: HelloPayload, token: str | None, *, source: str
    ) -> AuthResult:
        if verify_token(token, self._hash):
            return AuthResult(ok=True)
        return AuthResult(ok=False, reason="authentication failed")

    def authenticate_message(self, agent_id: str, token: str | None) -> bool:
        return verify_token(token, self._hash)
