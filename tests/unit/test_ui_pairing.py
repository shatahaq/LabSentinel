"""UI bridge methods behind the "Pemasangan PC" card."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from labsentinel_dashboard.app import EXPOSED
from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.pairing import PairingService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.ui.bridge import UiBridge


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "ui.sqlite")
    yield d
    d.close()


def _bridge(db: Database, pairing: PairingService | None) -> UiBridge:
    unused: Any = None  # pairing methods never touch the agent-side services
    loop = asyncio.new_event_loop()
    loop.close()
    return UiBridge(
        service=unused, sessions=unused, rules=unused, db=db, bus=EventBus(),
        loop=loop, pairing=pairing,
    )


def test_pairing_methods_are_exposed_to_the_ui() -> None:
    for name in ("get_pairing", "rotate_pairing_code", "set_pairing_open"):
        assert name in EXPOSED
        assert callable(getattr(UiBridge, name))


def test_get_pairing_shows_code_state_and_paired_count(db: Database) -> None:
    pairing = PairingService(db)
    bridge = _bridge(db, pairing)
    res = bridge.get_pairing()
    assert res == {"ok": True, "code": pairing.code, "open": True, "paired_count": 0}

    pairing.pair(agent_id="pc-1", hostname="PC-1", code=pairing.code, source="10.0.0.1")
    pairing.pair(agent_id="pc-2", hostname="PC-2", code=pairing.code, source="10.0.0.2")
    assert bridge.get_pairing()["paired_count"] == 2


def test_get_pairing_never_returns_tokens(db: Database) -> None:
    pairing = PairingService(db)
    token = pairing.pair(
        agent_id="pc-1", hostname="PC-1", code=pairing.code, source="10.0.0.1"
    ).token
    assert token is not None
    assert token not in repr(_bridge(db, pairing).get_pairing())


def test_rotate_pairing_code_from_ui(db: Database) -> None:
    pairing = PairingService(db)
    bridge = _bridge(db, pairing)
    before = pairing.code
    res = bridge.rotate_pairing_code()
    assert res["ok"]
    assert res["code"] == pairing.code
    assert len(res["code"]) == 6
    entry = next(r for r in db.list_audit() if r.action == "pairing.rotate")
    assert entry.actor == "pengawas"
    # (A collision with the old code is possible but irrelevant here.)
    assert before.isdigit()


def test_close_and_reopen_pairing_from_ui(db: Database) -> None:
    pairing = PairingService(db)
    bridge = _bridge(db, pairing)
    res = bridge.set_pairing_open(False)
    assert res["ok"] and res["open"] is False
    assert not pairing.is_open
    assert not pairing.pair(
        agent_id="pc-1", hostname="PC-1", code=pairing.code, source="10.0.0.1"
    ).ok

    res = bridge.set_pairing_open(True)
    assert res["ok"] and res["open"] is True
    actions = [r.action for r in db.list_audit()]
    assert "pairing.close" in actions and "pairing.open" in actions


def test_bridge_without_pairing_reports_an_error_instead_of_raising(db: Database) -> None:
    bridge = _bridge(db, None)
    for res in (bridge.get_pairing(), bridge.rotate_pairing_code(), bridge.set_pairing_open(True)):
        assert res["ok"] is False
        assert res["error"]
