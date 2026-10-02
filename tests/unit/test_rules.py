"""Rule model + whitelist default tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from labsentinel_shared.enums import EnforcementMode
from labsentinel_shared.rules import (
    DEFAULT_WHITELIST,
    PROTECTED_PROCESSES,
    AppRule,
    RuleSet,
    SiteRule,
)


def test_process_name_normalized() -> None:
    assert AppRule(process_name="  Code.EXE ").process_name == "code.exe"


def test_sha256_validation() -> None:
    AppRule(process_name="chrome.exe", expected_sha256="a" * 64)
    with pytest.raises(ValueError):
        AppRule(process_name="x.exe", expected_sha256="too-short")
    with pytest.raises(ValueError):
        AppRule(process_name="x.exe", expected_sha256="z" * 64)  # non-hex


def test_effective_whitelist_always_includes_defaults() -> None:
    rs = RuleSet(revision=1, app_whitelist=(AppRule(process_name="code.exe"),))
    eff = rs.effective_whitelist_names()
    assert eff >= DEFAULT_WHITELIST
    assert "code.exe" in eff


def test_protected_processes_cover_critical_system() -> None:
    for critical in ("csrss.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe"):
        assert critical in PROTECTED_PROCESSES


def test_first_install_is_dry_run_by_default() -> None:
    assert RuleSet(revision=0).dry_run is True


def test_exam_defaults_strict() -> None:
    rs = RuleSet.exam_defaults(revision=2)
    assert rs.mode is EnforcementMode.EXAM
    assert rs.dry_run is False
    assert rs.block_incognito is True


def test_site_keywords_lowercased() -> None:
    s = SiteRule(pattern="*.example.com", keywords=("Game", " ChatBOT "))
    assert s.keywords == ("game", "chatbot")


def test_ruleset_is_frozen() -> None:
    rs = RuleSet(revision=1)
    with pytest.raises(ValidationError):
        rs.revision = 2  # type: ignore[misc]
