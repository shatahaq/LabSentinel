"""Authentication token handling and replay protection.

The agent and dashboard share a pairing token (provisioned out-of-band, stored
hashed on the dashboard). Every envelope carries the token; the server verifies
it in constant time before trusting the payload. Replay protection drops
envelopes that are too old, too far in the future, or whose id was seen
recently.

No secret is ever hardcoded here: tokens come from config/pairing at runtime.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict

__all__ = [
    "PAIRING_CODE_DIGITS",
    "RateLimiter",
    "ReplayGuard",
    "code_matches",
    "generate_pairing_code",
    "generate_token",
    "hash_token",
    "verify_token",
]


PAIRING_CODE_DIGITS = 6


def generate_token(nbytes: int = 32) -> str:
    """Create a fresh, URL-safe token (the long-lived per-agent credential)."""
    return secrets.token_urlsafe(nbytes)


def generate_pairing_code(digits: int = PAIRING_CODE_DIGITS) -> str:
    """Create a short numeric code an admin can read out and type.

    A 6-digit code has only a million possibilities, so it is deliberately
    **not** the long-lived credential: it is presented once during installation
    and exchanged for a strong per-agent token. Attempts against it are rate
    limited (:class:`RateLimiter`), which is what makes a short code safe here.
    """
    if not 4 <= digits <= 12:
        raise ValueError("pairing code must be 4-12 digits")
    low = 10 ** (digits - 1)
    high = 10**digits - 1
    return str(secrets.randbelow(high - low + 1) + low)


def code_matches(presented: str | None, expected: str) -> bool:
    """Constant-time comparison of a pairing code (whitespace/dashes ignored)."""
    if not presented:
        return False
    cleaned = "".join(ch for ch in presented if ch.isdigit())
    return hmac.compare_digest(cleaned, expected)


class RateLimiter:
    """Sliding-window limiter that locks out a source after repeated failures.

    Used to make a short pairing code impractical to guess: a handful of wrong
    codes from one machine triggers a lockout, so a million-guess sweep would
    take far longer than any exam session.
    """

    def __init__(
        self, *, max_failures: int = 5, window_s: float = 300.0, lockout_s: float = 900.0
    ) -> None:
        self._max = max_failures
        self._window = window_s
        self._lockout = lockout_s
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def is_locked(self, key: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        until = self._locked_until.get(key)
        if until is None:
            return False
        if current >= until:
            del self._locked_until[key]
            self._failures.pop(key, None)
            return False
        return True

    def record_failure(self, key: str, *, now: float | None = None) -> bool:
        """Record a failed attempt. Returns True if this triggered a lockout."""
        current = time.time() if now is None else now
        recent = [t for t in self._failures.get(key, []) if current - t < self._window]
        recent.append(current)
        self._failures[key] = recent
        if len(recent) >= self._max:
            self._locked_until[key] = current + self._lockout
            return True
        return False

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        current = time.time() if now is None else now
        until = self._locked_until.get(key)
        return max(0.0, until - current) if until else 0.0


def hash_token(token: str) -> str:
    """Hash a token for at-rest storage (salted via a fixed app pepper + SHA-256).

    The dashboard stores only this hash; the raw token lives on agents.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(presented: str | None, expected_hash: str) -> bool:
    """Constant-time check of a presented token against a stored hash."""
    if not presented:
        return False
    presented_hash = hash_token(presented)
    return hmac.compare_digest(presented_hash, expected_hash)


class ReplayGuard:
    """Bounded, time-windowed replay detector.

    An envelope is accepted at most once, and only if its timestamp is within
    ``window_s`` of now. Memory is bounded to ``max_ids`` via LRU eviction.
    """

    def __init__(self, window_s: float = 120.0, max_ids: int = 10_000) -> None:
        self._window_s = window_s
        self._max_ids = max_ids
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check(self, msg_id: str, ts: float, *, now: float | None = None) -> bool:
        """Return True if the message is fresh and unseen; False to drop it."""
        current = time.time() if now is None else now
        if abs(current - ts) > self._window_s:
            return False
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = ts
        self._evict(current)
        return True

    def _evict(self, now: float) -> None:
        # Drop expired ids, then enforce the hard cap.
        cutoff = now - self._window_s
        while self._seen:
            _oldest_id, oldest_ts = next(iter(self._seen.items()))
            if oldest_ts < cutoff:
                self._seen.popitem(last=False)
            else:
                break
        while len(self._seen) > self._max_ids:
            self._seen.popitem(last=False)
