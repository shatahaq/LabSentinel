"""Browser policy tests — above all, that institution policy is never damaged."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from labsentinel_agent.policy.browser_policy import (
    INCOGNITO_DISABLED,
    MARKER_PATH,
    BrowserPolicyManager,
)
from labsentinel_agent.policy.registry import FakeRegistry, Hive, ValueType

CHROME_BL = r"SOFTWARE\Policies\Google\Chrome\URLBlocklist"
CHROME_POL = r"SOFTWARE\Policies\Google\Chrome"
EDGE_BL = r"SOFTWARE\Policies\Microsoft\Edge\URLBlocklist"
EDGE_POL = r"SOFTWARE\Policies\Microsoft\Edge"


def _mgr(tmp_path: Path, reg: FakeRegistry) -> BrowserPolicyManager:
    return BrowserPolicyManager(reg, backup_path=tmp_path / "policy_backup.json")


def _blocklist(reg: FakeRegistry, path: str) -> list[str]:
    vals = reg.read_values(Hive.HKLM, path)
    return [v for _, (v, _t) in sorted(vals.items(), key=lambda kv: int(kv[0]))]


def _seed_institution(reg: FakeRegistry) -> None:
    """An institution policy that already exists on the lab PC."""
    reg.create_key(Hive.HKLM, CHROME_BL)
    reg.write_value(Hive.HKLM, CHROME_BL, "1", "torrent-site.example", ValueType.SZ)
    reg.write_value(Hive.HKLM, CHROME_BL, "2", "malware.example", ValueType.SZ)
    reg.write_value(Hive.HKLM, CHROME_POL, "HomepageLocation", "kampus.ac.id", ValueType.SZ)


# --------------------------------------------------------------------- apply
def test_apply_writes_blocklist_to_both_browsers(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _mgr(tmp_path, reg).apply(["game.example", "sosmed.example"])
    assert _blocklist(reg, CHROME_BL) == ["game.example", "sosmed.example"]
    assert _blocklist(reg, EDGE_BL) == ["game.example", "sosmed.example"]


def test_apply_disables_incognito(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _mgr(tmp_path, reg).apply(["x.example"], block_incognito=True)
    assert reg.read_values(Hive.HKLM, CHROME_POL)["IncognitoModeAvailability"] == (
        INCOGNITO_DISABLED,
        ValueType.DWORD,
    )
    assert reg.read_values(Hive.HKLM, EDGE_POL)["InPrivateModeAvailability"] == (
        INCOGNITO_DISABLED,
        ValueType.DWORD,
    )


def test_patterns_are_deduped_and_trimmed(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _mgr(tmp_path, reg).apply(["  a.example ", "a.example", "", "   ", "b.example"])
    assert _blocklist(reg, CHROME_BL) == ["a.example", "b.example"]


def test_reapply_does_not_accumulate_stale_entries(tmp_path: Path) -> None:
    reg = FakeRegistry()
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["a.example", "b.example", "c.example"])
    mgr.apply(["a.example"])  # smaller ruleset
    assert _blocklist(reg, CHROME_BL) == ["a.example"]


# ------------------------------------------------- institution policy safety
def test_institution_entries_are_preserved_and_keep_priority(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _seed_institution(reg)
    _mgr(tmp_path, reg).apply(["game.example"])
    entries = _blocklist(reg, CHROME_BL)
    assert entries[:2] == ["torrent-site.example", "malware.example"]  # institution first
    assert "game.example" in entries


def test_restore_returns_exact_original_state(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _seed_institution(reg)
    before = reg.read_values(Hive.HKLM, CHROME_BL)
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["game.example", "sosmed.example"])
    mgr.clear()
    assert reg.read_values(Hive.HKLM, CHROME_BL) == before
    # Unrelated institution policy untouched throughout.
    assert reg.read_values(Hive.HKLM, CHROME_POL)["HomepageLocation"] == (
        "kampus.ac.id",
        ValueType.SZ,
    )


def test_clear_removes_key_that_did_not_exist_before(tmp_path: Path) -> None:
    reg = FakeRegistry()  # no institution blocklist at all
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["game.example"])
    assert reg.key_exists(Hive.HKLM, CHROME_BL)
    mgr.clear()
    assert not reg.key_exists(Hive.HKLM, CHROME_BL)


def test_clear_restores_pre_existing_incognito_setting(tmp_path: Path) -> None:
    reg = FakeRegistry()
    # Institution deliberately ALLOWS incognito (value 0).
    reg.write_value(Hive.HKLM, CHROME_POL, "IncognitoModeAvailability", 0, ValueType.DWORD)
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["x.example"], block_incognito=True)
    assert reg.read_values(Hive.HKLM, CHROME_POL)["IncognitoModeAvailability"][0] == 1
    mgr.clear()
    assert reg.read_values(Hive.HKLM, CHROME_POL)["IncognitoModeAvailability"][0] == 0


def test_clear_removes_incognito_value_we_introduced(tmp_path: Path) -> None:
    reg = FakeRegistry()
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["x.example"], block_incognito=True)
    mgr.clear()
    assert "IncognitoModeAvailability" not in reg.read_values(Hive.HKLM, CHROME_POL)


def test_backup_captured_once_so_reapply_cannot_poison_it(tmp_path: Path) -> None:
    """A second apply must not snapshot our own entries as 'the original'."""
    reg = FakeRegistry()
    _seed_institution(reg)
    original = reg.read_values(Hive.HKLM, CHROME_BL)
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["game.example"])
    mgr.apply(["sosmed.example", "chatbot.example"])
    mgr.clear()
    assert reg.read_values(Hive.HKLM, CHROME_BL) == original


# ------------------------------------------------------------ crash recovery
def test_backup_written_before_registry_is_touched(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _seed_institution(reg)
    backup = tmp_path / "policy_backup.json"
    mgr = BrowserPolicyManager(reg, backup_path=backup)
    mgr.apply(["game.example"])
    data = json.loads(backup.read_text(encoding="utf-8"))
    saved = data["browsers"]["chrome"]["blocklist"]
    assert saved == {"1": "torrent-site.example", "2": "malware.example"}


def test_recover_restores_after_abnormal_exit(tmp_path: Path) -> None:
    reg = FakeRegistry()
    _seed_institution(reg)
    original = reg.read_values(Hive.HKLM, CHROME_BL)
    backup = tmp_path / "policy_backup.json"

    # Run 1 applies policy, then "crashes" (no clear()).
    BrowserPolicyManager(reg, backup_path=backup).apply(["game.example"])
    assert "game.example" in _blocklist(reg, CHROME_BL)

    # Run 2 starts fresh, finds the leftover backup and restores it.
    mgr2 = BrowserPolicyManager(reg, backup_path=backup)
    assert mgr2.recover_if_needed() is True
    assert reg.read_values(Hive.HKLM, CHROME_BL) == original
    assert not backup.exists()


def test_recover_is_noop_without_backup(tmp_path: Path) -> None:
    reg = FakeRegistry()
    assert _mgr(tmp_path, reg).recover_if_needed() is False


def test_corrupt_backup_blocks_further_changes(tmp_path: Path) -> None:
    """Rather than guess, refuse to touch policy when the backup is unreadable."""
    reg = FakeRegistry()
    _seed_institution(reg)
    backup = tmp_path / "policy_backup.json"
    backup.write_text("{not json", encoding="utf-8")
    mgr = BrowserPolicyManager(reg, backup_path=backup)
    mgr.clear()  # must not wipe institution policy
    assert _blocklist(reg, CHROME_BL) == ["torrent-site.example", "malware.example"]


# ------------------------------------------------------------- failure modes
def test_access_denied_keeps_backup_for_a_later_retry(tmp_path: Path) -> None:
    """Without admin rights the restore fails; the backup must survive."""
    reg = FakeRegistry()
    backup = tmp_path / "policy_backup.json"
    BrowserPolicyManager(reg, backup_path=backup).apply(["game.example"])

    locked = FakeRegistry(readonly_paths=frozenset({r"SOFTWARE\Policies"}))
    mgr = BrowserPolicyManager(locked, backup_path=backup)
    mgr.clear()
    assert backup.exists()  # not discarded, so an elevated run can still restore


def test_access_denied_on_apply_does_not_crash(tmp_path: Path) -> None:
    reg = FakeRegistry(readonly_paths=frozenset({r"SOFTWARE\Policies"}))
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["game.example"])  # logs an error, keeps running
    assert mgr.state.applied is True


def test_marker_key_reflects_management(tmp_path: Path) -> None:
    reg = FakeRegistry()
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["a.example", "b.example"])
    marker = reg.read_values(Hive.HKLM, MARKER_PATH)
    assert marker["Managing"][0] == 1
    assert marker["BlockedPatternCount"][0] == 2
    mgr.clear()
    assert reg.read_values(Hive.HKLM, MARKER_PATH)["Managing"][0] == 0


def test_blocklist_truncated_at_browser_limit(tmp_path: Path) -> None:
    reg = FakeRegistry()
    mgr = _mgr(tmp_path, reg)
    mgr.apply([f"site{i}.example" for i in range(1200)])
    assert len(_blocklist(reg, CHROME_BL)) == 900


def test_state_tracks_applied_and_cleared(tmp_path: Path) -> None:
    reg = FakeRegistry()
    mgr = _mgr(tmp_path, reg)
    assert mgr.state.applied is False
    mgr.apply(["a.example"])
    assert mgr.state.applied is True and mgr.state.patterns == ("a.example",)
    mgr.clear()
    assert mgr.state.applied is False


def test_non_numeric_institution_entries_survive(tmp_path: Path) -> None:
    reg = FakeRegistry()
    reg.create_key(Hive.HKLM, CHROME_BL)
    reg.write_value(Hive.HKLM, CHROME_BL, "1", "a.example", ValueType.SZ)
    reg.write_value(Hive.HKLM, CHROME_BL, "custom", "b.example", ValueType.SZ)
    mgr = _mgr(tmp_path, reg)
    mgr.apply(["ours.example"])
    assert set(_blocklist(reg, CHROME_BL)) == {"a.example", "b.example", "ours.example"}
    mgr.clear()
    restored = reg.read_values(Hive.HKLM, CHROME_BL)
    assert restored["custom"] == ("b.example", ValueType.SZ)


@pytest.mark.parametrize("block", [True, False])
def test_incognito_flag_respected(tmp_path: Path, block: bool) -> None:
    reg = FakeRegistry()
    _mgr(tmp_path, reg).apply(["a.example"], block_incognito=block)
    present = "IncognitoModeAvailability" in reg.read_values(Hive.HKLM, CHROME_POL)
    assert present is block
