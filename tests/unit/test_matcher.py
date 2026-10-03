"""Whitelist matcher + layered verification tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from labsentinel_agent.enforce.identity import HashCache, ProcessInfo
from labsentinel_agent.enforce.matcher import MatchVerdict, WhitelistMatcher
from labsentinel_shared.rules import AppRule, RuleSet


def _proc(name: str, path: str = "", pid: int = 100) -> ProcessInfo:
    return ProcessInfo(pid=pid, name=name, exe_path=path or f"C:/x/{name}")


def test_default_browser_allowed_by_name() -> None:
    rs = RuleSet(revision=1)
    assert WhitelistMatcher().check(_proc("chrome.exe"), rs).verdict is MatchVerdict.ALLOWED


def test_non_whitelisted_is_violation() -> None:
    rs = RuleSet(revision=1)
    r = WhitelistMatcher().check(_proc("game.exe"), rs)
    assert r.verdict is MatchVerdict.NOT_WHITELISTED
    assert r.is_violation


def test_whitelisted_by_explicit_rule() -> None:
    rs = RuleSet(revision=1, app_whitelist=(AppRule(process_name="code.exe"),))
    assert WhitelistMatcher().check(_proc("code.exe"), rs).verdict is MatchVerdict.ALLOWED


def test_path_pinned_rule(tmp_path: Path) -> None:
    rs = RuleSet(
        revision=1,
        app_whitelist=(
            AppRule(process_name="code.exe", expected_path_contains="Microsoft VS Code"),
        ),
    )
    good = _proc("code.exe", path="C:/Program Files/Microsoft VS Code/Code.exe")
    bad = _proc("code.exe", path="C:/Games/Code.exe")
    assert WhitelistMatcher().check(good, rs).verdict is MatchVerdict.ALLOWED
    assert WhitelistMatcher().check(bad, rs).verdict is MatchVerdict.IMPERSONATION


def test_hash_pinned_rule_detects_renamed_game(tmp_path: Path) -> None:
    # Real "browser" binary and an impostor renamed to chrome.exe.
    real = tmp_path / "chrome.exe"
    real.write_bytes(b"the real browser bytes")
    real_hash = hashlib.sha256(real.read_bytes()).hexdigest()

    impostor = tmp_path / "evil" / "chrome.exe"
    impostor.parent.mkdir()
    impostor.write_bytes(b"actually a game")

    rs = RuleSet(
        revision=1,
        app_whitelist=(AppRule(process_name="chrome.exe", expected_sha256=real_hash),),
    )
    matcher = WhitelistMatcher(HashCache())

    ok = matcher.check(_proc("chrome.exe", path=str(real)), rs)
    assert ok.verdict is MatchVerdict.ALLOWED

    evil = matcher.check(_proc("chrome.exe", path=str(impostor)), rs)
    assert evil.verdict is MatchVerdict.IMPERSONATION
    assert "SHA-256 mismatch" in evil.reason


def test_hash_pinned_but_no_path_is_impersonation() -> None:
    rs = RuleSet(
        revision=1,
        app_whitelist=(AppRule(process_name="code.exe", expected_sha256="a" * 64),),
    )
    r = WhitelistMatcher().check(ProcessInfo(pid=1, name="code.exe", exe_path=None), rs)
    assert r.verdict is MatchVerdict.IMPERSONATION


def test_hashcache_rehashes_on_change(tmp_path: Path) -> None:
    f = tmp_path / "a.bin"
    f.write_bytes(b"one")
    cache = HashCache()
    h1 = cache.sha256(f)
    assert h1 == hashlib.sha256(b"one").hexdigest()
    # Second call hits the cache (same size/mtime) -> same value.
    assert cache.sha256(f) == h1
    # Change content (and mtime) -> re-hash.
    import os
    import time

    f.write_bytes(b"two-different")
    os.utime(f, (time.time() + 2, time.time() + 2))
    assert cache.sha256(f) == hashlib.sha256(b"two-different").hexdigest()


def test_hashcache_missing_file_returns_none(tmp_path: Path) -> None:
    assert HashCache().sha256(tmp_path / "nope.exe") is None
