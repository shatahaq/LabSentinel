"""Agent pairing and authentication.

An admin reads a **6-digit code** off the dashboard and types it once while
installing an agent. That short code is only ever used for that first contact:
on success the dashboard issues a long, random, per-agent token, stores its hash,
and the agent uses that token from then on.

Why not just use the 6-digit code as the password? Because a million
possibilities is guessable by anyone on the lab network. Three things make the
short code safe:

* it is exchanged immediately for a strong token, so it is not the standing
  credential;
* guesses are **rate limited per source address** and trigger a lockout, so a
  sweep would take far longer than any exam;
* it can be rotated at any time, and pairing can be closed entirely once all
  lab PCs are installed.

Each agent ends up with its own credential, so a token recovered from one PC
cannot be used to impersonate a different PC.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import AuthResult
from labsentinel_shared.protocol import HelloPayload
from labsentinel_shared.security import (
    RateLimiter,
    code_matches,
    generate_pairing_code,
    generate_token,
    hash_token,
    verify_token,
)

log = logging.getLogger("labsentinel.dashboard.pairing")

SETTING_CODE = "pairing_code"
SETTING_OPEN = "pairing_open"


@dataclass(frozen=True)
class PairingResult:
    ok: bool
    token: str | None = None
    reason: str = ""


class PairingService:
    def __init__(
        self,
        db: Database,
        *,
        limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._limiter = limiter or RateLimiter()
        self._clock = clock
        code = db.get_setting(SETTING_CODE)
        if not code:
            code = generate_pairing_code()
            db.set_setting(SETTING_CODE, code)
        self._code: str = str(code)
        stored_open = db.get_setting(SETTING_OPEN)
        self._open: bool = True if stored_open is None else bool(stored_open)

    # ------------------------------------------------------------- the code
    @property
    def code(self) -> str:
        with self._lock:
            return self._code

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def rotate_code(self, *, actor: str) -> str:
        """Issue a new code. Already-paired PCs keep working."""
        with self._lock:
            self._code = generate_pairing_code()
            self._db.set_setting(SETTING_CODE, self._code)
        self._db.audit(actor=actor, action="pairing.rotate", result="ok")
        return self._code

    def set_open(self, open_: bool, *, actor: str) -> None:
        """Close pairing once every lab PC is installed."""
        with self._lock:
            self._open = bool(open_)
            self._db.set_setting(SETTING_OPEN, self._open)
        self._db.audit(
            actor=actor,
            action="pairing.open" if open_ else "pairing.close",
            result="ok",
        )

    # ------------------------------------------------------- authentication
    # (implements the transport's Authenticator protocol)
    def authenticate_message(self, agent_id: str, token: str | None) -> bool:
        """Check an already-paired agent's own token on every message."""
        if not token:
            return False
        stored = self._db.get_agent_token_hash(agent_id)
        if stored is None:
            return False
        return verify_token(token, stored)

    def authenticate_hello(
        self, hello: HelloPayload, token: str | None, *, source: str
    ) -> AuthResult:
        """Authenticate first contact: a known token, or else a pairing code."""
        if self.authenticate_message(hello.agent_id, token):
            return AuthResult(ok=True)

        if token and not hello.pairing_code:
            # Presented a token we do not recognise: revoked, or the database
            # was reset. It must pair again rather than silently reconnecting.
            log.warning("agent %s presented an unknown token", hello.agent_id)
            return AuthResult(ok=False, reason="PC ini perlu dipasangkan ulang")

        result = self.pair(
            agent_id=hello.agent_id,
            hostname=hello.hostname,
            code=hello.pairing_code,
            source=source,
        )
        if result.ok:
            return AuthResult(ok=True, issued_token=result.token)
        return AuthResult(ok=False, reason=result.reason)

    def pair(self, *, agent_id: str, hostname: str, code: str | None, source: str) -> PairingResult:
        """Exchange a pairing code for this agent's own long-lived token."""
        now = self._clock()
        if self._limiter.is_locked(source, now=now):
            wait = int(self._limiter.retry_after(source, now=now))
            log.warning("pairing from %s is locked out for %ds", source, wait)
            return PairingResult(False, reason=f"terlalu banyak percobaan, tunggu {wait} detik")

        with self._lock:
            open_ = self._open
            expected = self._code

        if not open_:
            self._db.audit(
                actor="sistem", action="pairing.refused", target=hostname,
                details={"agent_id": agent_id, "source": source},
                result="pendaftaran ditutup",
            )
            return PairingResult(False, reason="pendaftaran PC baru sedang ditutup")

        if not code_matches(code, expected):
            locked = self._limiter.record_failure(source, now=now)
            log.warning(
                "wrong pairing code from %s for %s%s",
                source, hostname, " (now locked out)" if locked else "",
            )
            self._db.audit(
                actor="sistem", action="pairing.failed", target=hostname,
                details={"agent_id": agent_id, "source": source, "locked_out": locked},
                result="kode salah",
            )
            return PairingResult(False, reason="kode pemasangan salah")

        self._limiter.record_success(source)
        token = generate_token()
        self._db.set_agent_token_hash(agent_id, hash_token(token))
        self._db.audit(
            actor="sistem", action="pairing.success", target=hostname,
            details={"agent_id": agent_id, "source": source}, result="ok",
        )
        log.info("paired agent %s (%s) from %s", agent_id, hostname, source)
        return PairingResult(True, token=token)

    def unpair(self, agent_id: str, *, actor: str) -> None:
        """Revoke a PC's credential; it must be paired again to reconnect."""
        self._db.delete_agent_token(agent_id)
        self._db.audit(actor=actor, action="pairing.revoke", target=agent_id, result="ok")
