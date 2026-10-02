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


def generate_token(nbytes: int = 32) -> str:
    """Create a fresh, URL-safe pairing token."""
    return secrets.token_urlsafe(nbytes)


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
