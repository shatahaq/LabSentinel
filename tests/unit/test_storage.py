"""SQLite storage, event bus and rules service tests."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.enums import EnforcementMode
from labsentinel_shared.rules import AppRule, RuleSet


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.sqlite")
    yield d
    d.close()


def test_agent_upsert_and_list(db: Database) -> None:
    db.upsert_agent(
        agent_id="pc-01", hostname="LAB-01", username="s1", agent_version="0.1",
        remote_addr="10.0.0.2:5000", now=100.0,
    )
    db.upsert_agent(
        agent_id="pc-01", hostname="LAB-01", username="s2", agent_version="0.2",
        remote_addr="10.0.0.2:5001", now=200.0,
    )
    agents = db.list_agents()
    assert len(agents) == 1
    assert agents[0]["username"] == "s2"
    assert agents[0]["first_seen"] == 100.0  # preserved on update
    assert agents[0]["last_seen"] == 200.0


def test_violation_roundtrip_and_filters(db: Database) -> None:
    sid = db.create_session(name="Ujian", mode="exam", status="active", created_by="dosen")
    for i, sess in enumerate([sid, None, sid]):
        db.add_violation(
            agent_id="pc-01", hostname="LAB-01", kind="app_blocked", subject=f"game{i}.exe",
            detail="", detected_from_title=False, dry_run=False, occurred_at=1000.0 + i,
            screenshot_path=None, session_id=sess,
        )
    assert len(db.list_violations()) == 3
    in_session = db.list_violations(session_id=sid)
    assert [v.subject for v in in_session] == ["game2.exe", "game0.exe"]  # newest first
    assert len(db.list_violations(since=1001.0)) == 2


def test_sql_injection_is_inert(db: Database) -> None:
    evil = "x'); DROP TABLE violations; --"
    db.add_violation(
        agent_id=evil, hostname=evil, kind="app_blocked", subject=evil, detail=evil,
        detected_from_title=False, dry_run=False, occurred_at=1.0, screenshot_path=None,
        session_id=None,
    )
    rows = db.list_violations()
    assert rows[0].subject == evil  # stored literally, table intact


def test_command_lifecycle_only_finishes_once(db: Database) -> None:
    db.add_command(command_id="c1", agent_id="pc-01", command="pause", issued_by="dosen",
                   issued_at=10.0)
    db.finish_command("c1", status="acked", resulting_state="paused", at=11.0)
    db.finish_command("c1", status="timeout", at=20.0)  # late timeout must not overwrite
    cmd = db.get_command("c1")
    assert cmd is not None
    assert cmd["status"] == "acked"
    assert cmd["resulting_state"] == "paused"


def test_audit_is_append_only_and_ordered(db: Database) -> None:
    db.audit(actor="dosen", action="command.pause", target="pc-01", details={"n": 1}, ts=1.0)
    db.audit(actor="dosen", action="command.resume", target="all", ts=2.0)
    rows = db.list_audit()
    assert [r.action for r in rows] == ["command.resume", "command.pause"]
    assert rows[1].details == {"n": 1}
    assert not hasattr(db, "update_audit_result")


def test_sessions(db: Database) -> None:
    sid = db.create_session(name="Praktikum", mode="lab", status="scheduled", created_by="a",
                            scheduled_start=100.0, scheduled_end=200.0)
    db.set_session_status(sid, "active", started_at=101.0)
    db.set_session_status(sid, "ended", ended_at=199.0)
    s = db.get_session(sid)
    assert s is not None
    assert (s.status, s.started_at, s.ended_at) == ("ended", 101.0, 199.0)
    assert [x.id for x in db.list_sessions(status="ended")] == [sid]


def test_settings_json(db: Database) -> None:
    assert db.get_setting("missing", default=5) == 5
    db.set_setting("paused_agents", ["pc-01", "pc-02"])
    db.set_setting("paused_agents", ["pc-03"])
    assert db.get_setting("paused_agents") == ["pc-03"]


def test_retention_purge_returns_screenshot_paths(db: Database) -> None:
    for t, path in [(10.0, "old.jpg"), (20.0, None), (500.0, "new.jpg")]:
        db.add_violation(
            agent_id="a", hostname="h", kind="app_blocked", subject="s", detail="",
            detected_from_title=False, dry_run=False, occurred_at=t, screenshot_path=path,
            session_id=None,
        )
    db.audit(actor="x", action="y", ts=10.0)
    paths = db.purge_older_than(100.0)
    assert paths == ["old.jpg"]
    assert [v.screenshot_path for v in db.list_violations()] == ["new.jpg"]
    assert db.list_audit() == []


def test_concurrent_writes_from_threads(db: Database) -> None:
    def worker(n: int) -> None:
        for i in range(50):
            db.audit(actor=f"t{n}", action="x", details={"i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(db.list_audit(limit=1000)) == 200


def test_event_bus_isolates_failing_subscriber() -> None:
    bus = EventBus()
    got: list[str] = []

    def bad(topic: str, data: dict[str, object]) -> None:
        raise RuntimeError("boom")

    bus.subscribe(bad)
    unsub = bus.subscribe(lambda t, d: got.append(t))
    bus.publish("a", {})
    unsub()
    bus.publish("b", {})
    assert got == ["a"]


def test_rules_service_revisions_persist(db: Database) -> None:
    rs = RulesService(db, default=RuleSet(revision=0))
    assert rs.current().revision == 0
    new = rs.update(actor="dosen", mode=EnforcementMode.EXAM, dry_run=False)
    assert new.revision == 1 and new.mode is EnforcementMode.EXAM

    # A fresh service (dashboard restart) resumes from the stored revision.
    again = RulesService(db, default=RuleSet(revision=0))
    assert again.current().revision == 1
    assert again.current().dry_run is False
    assert any(a.action == "rules.update" for a in db.list_audit())


def test_rules_replace_keeps_revision_monotonic(db: Database) -> None:
    rs = RulesService(db, default=RuleSet(revision=5))
    imported = RuleSet(revision=0, app_whitelist=(AppRule(process_name="code.exe"),))
    out = rs.replace(imported, actor="dosen")
    assert out.revision == 6
    assert "code.exe" in out.effective_whitelist_names()
