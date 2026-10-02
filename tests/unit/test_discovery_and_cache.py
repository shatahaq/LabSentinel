"""UDP beacon parsing + rule cache persistence tests (no sockets)."""

from __future__ import annotations

from pathlib import Path

from labsentinel_agent.rule_cache import RuleCache
from labsentinel_shared.discovery import Beacon, is_probe, parse_beacon
from labsentinel_shared.enums import EnforcementMode
from labsentinel_shared.rules import AppRule, RuleSet


def test_beacon_roundtrip() -> None:
    b = Beacon(ws_host="192.168.1.10", ws_port=8765, dashboard_name="Lab A")
    parsed = parse_beacon(b.to_bytes())
    assert parsed == b
    assert parsed is not None and parsed.ws_url == "ws://192.168.1.10:8765"


def test_beacon_tls_url() -> None:
    b = Beacon(ws_host="10.0.0.1", ws_port=9000, dashboard_name="x", tls=True)
    assert b.ws_url == "wss://10.0.0.1:9000"


def test_parse_rejects_foreign_or_malformed() -> None:
    assert parse_beacon(b"not json") is None
    assert parse_beacon(b'{"service":"other"}') is None
    assert parse_beacon(b'{"service":"labsentinel","kind":"probe","v":1}') is None  # not a beacon
    assert parse_beacon(b'{"service":"labsentinel","kind":"beacon","v":1}') is None  # no host/port


def test_is_probe() -> None:
    assert is_probe(b'{"service":"labsentinel","kind":"probe","v":1}') is True
    assert is_probe(b'{"service":"labsentinel","kind":"beacon","v":1}') is False


def test_rule_cache_atomic_roundtrip(tmp_path: Path) -> None:
    cache = RuleCache(tmp_path / "rules.json")
    assert cache.load() is None  # nothing yet

    rs = RuleSet(
        revision=5,
        mode=EnforcementMode.LAB,
        app_whitelist=(AppRule(process_name="code.exe"),),
    )
    assert cache.update(rs) is True

    # Fresh instance reads it back.
    reloaded = RuleCache(tmp_path / "rules.json").load()
    assert reloaded is not None
    assert reloaded.revision == 5
    assert "code.exe" in reloaded.effective_whitelist_names()


def test_rule_cache_ignores_stale_revision(tmp_path: Path) -> None:
    cache = RuleCache(tmp_path / "rules.json")
    cache.update(RuleSet(revision=10))
    assert cache.update(RuleSet(revision=4)) is False  # older, ignored
    assert cache.current is not None and cache.current.revision == 10


def test_rule_cache_survives_corrupt_file(tmp_path: Path) -> None:
    p = tmp_path / "rules.json"
    p.write_text("{garbage", encoding="utf-8")
    assert RuleCache(p).load() is None  # no crash
