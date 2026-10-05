"""Pairing tests: the 6-digit code, rate limiting and per-agent tokens.

Covers the dashboard's :class:`PairingService` and the primitives it is built on
(``generate_pairing_code``, ``code_matches``, ``RateLimiter``). The end-to-end
path over a real WebSocket lives in tests/integration/test_pairing_flow.py.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from labsentinel_dashboard.domain.pairing import SETTING_CODE, SETTING_OPEN, PairingService
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.protocol import (
    Envelope,
    HelloPayload,
    WelcomePayload,
    decode,
    encode,
)
from labsentinel_shared.rules import RuleSet
from labsentinel_shared.security import (
    RateLimiter,
    code_matches,
    generate_pairing_code,
    hash_token,
)

SOURCE = "10.0.0.21"


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "pairing.sqlite")
    yield d
    d.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def svc(db: Database, clock: FakeClock) -> PairingService:
    return PairingService(db, clock=clock)


def _hello(agent_id: str = "lab-pc-01", *, code: str | None = None) -> HelloPayload:
    return HelloPayload(agent_id=agent_id, hostname=agent_id.upper(), pairing_code=code)


def _wrong(code: str) -> str:
    """A 6-digit code guaranteed to differ from ``code``."""
    return "100000" if code != "100000" else "100001"


# ----------------------------------------------------------- primitives
def test_generated_code_is_six_digits_without_leading_zero() -> None:
    for _ in range(500):
        code = generate_pairing_code()
        assert len(code) == 6
        assert code.isdigit()
        assert code[0] != "0"  # an admin reading "012345" aloud drops the zero


def test_generated_codes_vary() -> None:
    assert len({generate_pairing_code() for _ in range(200)}) > 150


@pytest.mark.parametrize("digits", [3, 13])
def test_code_length_is_bounded(digits: int) -> None:
    with pytest.raises(ValueError):
        generate_pairing_code(digits)


@pytest.mark.parametrize("typed", ["482913", " 482913 ", "482-913", "482 913"])
def test_code_matches_ignores_spacing_and_dashes(typed: str) -> None:
    assert code_matches(typed, "482913")


@pytest.mark.parametrize("typed", [None, "", "482914", "48291", "4829130", "abcdef"])
def test_code_matches_rejects_wrong_or_empty(typed: str | None) -> None:
    assert not code_matches(typed, "482913")


def test_rate_limiter_locks_after_max_failures() -> None:
    rl = RateLimiter(max_failures=5, window_s=300, lockout_s=900)
    for i in range(4):
        assert rl.record_failure("a", now=100.0 + i) is False
        assert not rl.is_locked("a", now=100.0 + i)
    assert rl.record_failure("a", now=105.0) is True
    assert rl.is_locked("a", now=105.0)
    assert rl.retry_after("a", now=105.0) == pytest.approx(900.0)
    # Another machine is unaffected.
    assert not rl.is_locked("b", now=105.0)


def test_rate_limiter_lockout_expires_and_resets_count() -> None:
    rl = RateLimiter(max_failures=5, window_s=300, lockout_s=900)
    for i in range(5):
        rl.record_failure("a", now=float(i))
    assert rl.is_locked("a", now=899.0)
    assert not rl.is_locked("a", now=905.0)
    assert rl.retry_after("a", now=905.0) == 0.0
    # The slate is clean: one more failure does not immediately re-lock.
    assert rl.record_failure("a", now=906.0) is False


def test_rate_limiter_forgets_failures_outside_window() -> None:
    rl = RateLimiter(max_failures=5, window_s=300, lockout_s=900)
    for i in range(4):
        rl.record_failure("a", now=float(i))
    # Long after the window, earlier failures no longer count.
    assert rl.record_failure("a", now=1000.0) is False
    assert not rl.is_locked("a", now=1000.0)


def test_rate_limiter_success_clears_failures() -> None:
    rl = RateLimiter(max_failures=5)
    for i in range(4):
        rl.record_failure("a", now=float(i))
    rl.record_success("a")
    assert rl.record_failure("a", now=5.0) is False


# ------------------------------------------------------------ the code
def test_code_is_created_once_and_survives_restart(db: Database, clock: FakeClock) -> None:
    first = PairingService(db, clock=clock)
    assert len(first.code) == 6
    assert db.get_setting(SETTING_CODE) == first.code
    assert PairingService(db, clock=clock).code == first.code


def test_pairing_is_open_by_default(svc: PairingService) -> None:
    assert svc.is_open


def test_rotate_code_issues_new_code_and_old_one_stops_working(
    svc: PairingService, db: Database
) -> None:
    old = svc.code
    new = svc.rotate_code(actor="pengawas")
    while new == old:  # one-in-a-million collision; rotate again
        new = svc.rotate_code(actor="pengawas")
    assert svc.code == new
    assert db.get_setting(SETTING_CODE) == new

    assert not svc.pair(agent_id="pc-1", hostname="PC-1", code=old, source=SOURCE).ok
    assert svc.pair(agent_id="pc-1", hostname="PC-1", code=new, source=SOURCE).ok
    assert any(r.action == "pairing.rotate" for r in db.list_audit())


def test_rotate_code_keeps_already_paired_agents(svc: PairingService) -> None:
    paired = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE)
    assert paired.token is not None
    svc.rotate_code(actor="pengawas")
    assert svc.authenticate_message("pc-1", paired.token)


# --------------------------------------------------------- wrong codes
def test_wrong_code_is_rejected_and_audited(svc: PairingService, db: Database) -> None:
    result = svc.pair(agent_id="pc-1", hostname="PC-1", code=_wrong(svc.code), source=SOURCE)
    assert not result.ok
    assert result.token is None
    assert "salah" in result.reason
    assert db.get_agent_token_hash("pc-1") is None

    entry = next(r for r in db.list_audit() if r.action == "pairing.failed")
    assert entry.target == "PC-1"
    assert entry.details["source"] == SOURCE
    # The code that was tried is never written to the audit log.
    assert svc.code not in json.dumps(entry.details)


def test_missing_code_is_rejected(svc: PairingService) -> None:
    assert not svc.pair(agent_id="pc-1", hostname="PC-1", code=None, source=SOURCE).ok


def test_lockout_after_five_wrong_codes_per_source(svc: PairingService, db: Database) -> None:
    bad = _wrong(svc.code)
    for _ in range(5):
        assert not svc.pair(agent_id="pc-1", hostname="PC-1", code=bad, source=SOURCE).ok

    # Now even the right code is refused from that address...
    locked = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE)
    assert not locked.ok
    assert "tunggu" in locked.reason
    assert db.get_agent_token_hash("pc-1") is None
    assert any(r.details.get("locked_out") for r in db.list_audit())

    # ...but a different PC on the network can still pair.
    other = svc.pair(agent_id="pc-2", hostname="PC-2", code=svc.code, source="10.0.0.22")
    assert other.ok


def test_lockout_is_per_address_not_per_agent_id(svc: PairingService) -> None:
    """Changing the claimed agent id must not reset the guess counter."""
    bad = _wrong(svc.code)
    for i in range(5):
        svc.pair(agent_id=f"fake-{i}", hostname=f"FAKE-{i}", code=bad, source=SOURCE)
    assert not svc.pair(agent_id="new", hostname="NEW", code=svc.code, source=SOURCE).ok


def test_lockout_expires(svc: PairingService, clock: FakeClock) -> None:
    bad = _wrong(svc.code)
    for _ in range(5):
        svc.pair(agent_id="pc-1", hostname="PC-1", code=bad, source=SOURCE)
    clock.advance(899)
    assert not svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).ok
    clock.advance(2)
    assert svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).ok


def test_four_wrong_codes_then_right_one_succeeds(svc: PairingService) -> None:
    bad = _wrong(svc.code)
    for _ in range(4):
        svc.pair(agent_id="pc-1", hostname="PC-1", code=bad, source=SOURCE)
    assert svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).ok


# --------------------------------------------------- per-agent tokens
def test_right_code_issues_token_and_stores_only_its_hash(
    svc: PairingService, db: Database
) -> None:
    result = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE)
    assert result.ok
    assert result.token is not None
    assert len(result.token) >= 40  # 32 random bytes, url-safe

    stored = db.get_agent_token_hash("pc-1")
    assert stored == hash_token(result.token)
    assert stored != result.token
    assert any(r.action == "pairing.success" for r in db.list_audit())


def test_each_agent_gets_its_own_token(svc: PairingService) -> None:
    a = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source="10.0.0.1")
    b = svc.pair(agent_id="pc-2", hostname="PC-2", code=svc.code, source="10.0.0.2")
    assert a.token and b.token and a.token != b.token
    # A token taken from one PC cannot speak for another.
    assert not svc.authenticate_message("pc-2", a.token)
    assert not svc.authenticate_message("pc-1", b.token)


def test_token_never_lands_in_database_or_audit(
    svc: PairingService, db: Database, tmp_path: Path
) -> None:
    result = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE)
    assert result.token is not None
    for row in db.list_audit():
        assert result.token not in json.dumps(row.details)
        assert result.token not in row.result
    db.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("pairing.sqlite*"))
    assert result.token.encode() not in raw


def test_authenticate_message(svc: PairingService) -> None:
    token = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    assert svc.authenticate_message("pc-1", token)
    assert not svc.authenticate_message("pc-1", None)
    assert not svc.authenticate_message("pc-1", "")
    assert not svc.authenticate_message("pc-1", "not-the-token")
    assert not svc.authenticate_message("unknown-pc", token)


# ----------------------------------------------------- authenticate_hello
def test_paired_agent_reconnects_with_its_token(svc: PairingService) -> None:
    token = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    result = svc.authenticate_hello(_hello("pc-1"), token, source=SOURCE)
    assert result.ok
    assert result.issued_token is None  # nothing new to store


def test_paired_agent_reconnects_even_when_pairing_is_closed(svc: PairingService) -> None:
    token = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    svc.set_open(False, actor="pengawas")
    svc.rotate_code(actor="pengawas")
    assert svc.authenticate_hello(_hello("pc-1"), token, source=SOURCE).ok


def test_hello_with_right_code_pairs_and_issues_token(
    svc: PairingService, db: Database
) -> None:
    result = svc.authenticate_hello(_hello("pc-9", code=svc.code), None, source=SOURCE)
    assert result.ok
    assert result.issued_token is not None
    assert db.get_agent_token_hash("pc-9") == hash_token(result.issued_token)


def test_hello_with_wrong_code_is_rejected(svc: PairingService) -> None:
    result = svc.authenticate_hello(_hello("pc-9", code=_wrong(svc.code)), None, source=SOURCE)
    assert not result.ok
    assert result.issued_token is None
    assert "salah" in result.reason


def test_hello_without_token_or_code_is_rejected(svc: PairingService) -> None:
    assert not svc.authenticate_hello(_hello("pc-9"), None, source=SOURCE).ok


def test_unknown_token_is_asked_to_pair_again(svc: PairingService, db: Database) -> None:
    result = svc.authenticate_hello(_hello("pc-1"), "some-old-token", source=SOURCE)
    assert not result.ok
    assert "dipasangkan ulang" in result.reason
    assert db.get_agent_token_hash("pc-1") is None


def test_unknown_token_does_not_count_as_a_guess(svc: PairingService) -> None:
    """A PC with a stale token retrying every few seconds must not lock out
    its own address before the admin gets to it."""
    for _ in range(20):
        svc.authenticate_hello(_hello("pc-1"), "some-old-token", source=SOURCE)
    assert svc.authenticate_hello(_hello("pc-1", code=svc.code), None, source=SOURCE).ok


def test_revoked_agent_must_pair_again(svc: PairingService, db: Database) -> None:
    token = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    svc.unpair("pc-1", actor="pengawas")
    assert db.get_agent_token_hash("pc-1") is None
    assert not svc.authenticate_message("pc-1", token)
    result = svc.authenticate_hello(_hello("pc-1"), token, source=SOURCE)
    assert not result.ok
    assert "dipasangkan ulang" in result.reason
    assert any(r.action == "pairing.revoke" for r in db.list_audit())


def test_repairing_replaces_the_old_token(svc: PairingService) -> None:
    old = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    new = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    assert old != new
    assert svc.authenticate_message("pc-1", new)
    assert not svc.authenticate_message("pc-1", old)


# ---------------------------------------------------------- closed pairing
def test_closed_pairing_refuses_new_pcs_even_with_right_code(
    svc: PairingService, db: Database
) -> None:
    svc.set_open(False, actor="pengawas")
    assert not svc.is_open
    result = svc.authenticate_hello(_hello("pc-new", code=svc.code), None, source=SOURCE)
    assert not result.ok
    assert "ditutup" in result.reason
    assert db.get_agent_token_hash("pc-new") is None
    actions = [r.action for r in db.list_audit()]
    assert "pairing.close" in actions
    assert "pairing.refused" in actions


def test_closed_pairing_does_not_count_toward_lockout(svc: PairingService) -> None:
    svc.set_open(False, actor="pengawas")
    for _ in range(10):
        svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE)
    svc.set_open(True, actor="pengawas")
    assert svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).ok


def test_open_state_survives_restart(db: Database, clock: FakeClock) -> None:
    PairingService(db, clock=clock).set_open(False, actor="pengawas")
    assert db.get_setting(SETTING_OPEN) is False
    assert not PairingService(db, clock=clock).is_open
    PairingService(db, clock=clock).set_open(True, actor="pengawas")
    assert PairingService(db, clock=clock).is_open


# ------------------------------------------------------------ redaction
def test_pairing_code_is_redacted_from_hello_logs() -> None:
    env = Envelope(payload=_hello("pc-1", code="482913"))
    safe = env.redacted()
    assert "482913" not in json.dumps(safe)
    assert safe["payload"]["pairing_code"] == "<redacted>"  # type: ignore[index]
    assert "482913" not in repr(env)
    assert "482913" not in repr(env.payload)


def test_envelope_token_and_issued_token_are_redacted() -> None:
    issued = "issued-per-agent-secret-0123456789abcdef"
    env = decode(
        encode(
            WelcomePayload(
                session_id="s1", server_time=1.0, ruleset=RuleSet(revision=1), agent_token=issued
            ),
            token="envelope-secret-token",
        )
    )
    dumped = json.dumps(env.redacted())
    assert issued not in dumped
    assert "envelope-secret-token" not in dumped
    assert "token" not in env.redacted()
    assert env.redacted()["payload"]["agent_token"] == "<redacted>"  # type: ignore[index]
    assert issued not in repr(env)
    assert "envelope-secret-token" not in repr(env)


def test_pairing_service_logs_never_contain_code_or_token(
    svc: PairingService, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bad = _wrong(svc.code)
    svc.pair(agent_id="pc-1", hostname="PC-1", code=bad, source=SOURCE)
    token = svc.pair(agent_id="pc-1", hostname="PC-1", code=svc.code, source=SOURCE).token
    svc.authenticate_hello(_hello("pc-2"), "stale-token-value", source=SOURCE)
    assert token is not None
    for text in (svc.code, bad, token, "stale-token-value"):
        assert text not in caplog.text
