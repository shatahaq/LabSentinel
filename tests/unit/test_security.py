"""Token auth + replay-guard tests."""

from __future__ import annotations

from labsentinel_shared.security import (
    ReplayGuard,
    generate_token,
    hash_token,
    verify_token,
)


def test_token_roundtrip() -> None:
    tok = generate_token()
    h = hash_token(tok)
    assert verify_token(tok, h) is True
    assert verify_token("wrong", h) is False
    assert verify_token(None, h) is False
    assert verify_token("", h) is False


def test_generated_tokens_are_unique() -> None:
    assert generate_token() != generate_token()


def test_replay_guard_blocks_duplicate() -> None:
    g = ReplayGuard(window_s=100)
    assert g.check("id-1", ts=1000.0, now=1000.0) is True
    assert g.check("id-1", ts=1000.0, now=1001.0) is False  # replay


def test_replay_guard_blocks_stale_and_future() -> None:
    g = ReplayGuard(window_s=10)
    assert g.check("old", ts=1000.0, now=1020.0) is False  # too old
    assert g.check("future", ts=1100.0, now=1000.0) is False  # too far ahead


def test_replay_guard_bounded_memory() -> None:
    g = ReplayGuard(window_s=10_000, max_ids=50)
    now = 1000.0
    for i in range(200):
        assert g.check(f"id-{i}", ts=now, now=now) is True
    # Internal store never exceeds the cap.
    assert len(g._seen) <= 50
