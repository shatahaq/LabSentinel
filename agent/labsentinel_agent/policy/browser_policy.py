"""Chrome/Edge site blocking via Windows policy registry keys.

Writes ``URLBlocklist`` entries (and disables incognito/InPrivate) so the browser
itself shows "blocked by your administrator" — no browser extension needed.

Protecting the institution's own policies
-----------------------------------------
A lab PC may already carry policies set by the institution. This manager must
never destroy them, so:

1. Before the **first** change, the complete original state of every key it will
   touch is captured and written to a backup file **atomically, before any
   registry write happens**. If the agent is killed mid-write, the backup on
   disk still describes the original state.
2. Institution entries are preserved and re-numbered first; Lab Sentinel's
   entries are appended after them.
3. Restoring rewrites exactly the captured original state — keys that did not
   exist before are removed entirely, keys that did are returned to their old
   values and numbering.
4. On startup a leftover backup means the previous run ended abnormally, so the
   manager restores it before doing anything else.

A marker key under ``HKLM\\SOFTWARE\\LabSentinel`` records that Lab Sentinel is
currently managing browser policy, so an administrator inspecting the machine
can see what is going on.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labsentinel_agent.policy.registry import Hive, RegistryError, RegistryPort, ValueType

log = logging.getLogger("labsentinel.agent.policy")

BACKUP_VERSION = 1
MARKER_PATH = r"SOFTWARE\LabSentinel\PolicyState"

# Chrome and Edge share the same policy shape under different roots.
BROWSERS: dict[str, dict[str, str]] = {
    "chrome": {
        "policy_path": r"SOFTWARE\Policies\Google\Chrome",
        "blocklist_path": r"SOFTWARE\Policies\Google\Chrome\URLBlocklist",
        "incognito_value": "IncognitoModeAvailability",
    },
    "edge": {
        "policy_path": r"SOFTWARE\Policies\Microsoft\Edge",
        "blocklist_path": r"SOFTWARE\Policies\Microsoft\Edge\URLBlocklist",
        "incognito_value": "InPrivateModeAvailability",
    },
}

# 1 = incognito/InPrivate disabled (Chrome and Edge use the same encoding).
INCOGNITO_DISABLED = 1

# Chrome/Edge cap URLBlocklist at 1000 entries; leave room for institution ones.
MAX_OUR_ENTRIES = 900


@dataclass(frozen=True)
class PolicyState:
    """What the manager believes is currently applied."""

    applied: bool
    patterns: tuple[str, ...] = ()
    incognito_blocked: bool = False


class BrowserPolicyManager:
    def __init__(
        self,
        registry: RegistryPort,
        *,
        backup_path: Path,
        browsers: dict[str, dict[str, str]] | None = None,
        clock: Any = time.time,
    ) -> None:
        self._reg = registry
        self._backup_path = backup_path
        self._browsers = browsers if browsers is not None else BROWSERS
        self._clock = clock
        self._state = PolicyState(applied=False)

    @property
    def state(self) -> PolicyState:
        return self._state

    @property
    def has_backup(self) -> bool:
        return self._backup_path.exists()

    # -- lifecycle ---------------------------------------------------------
    def recover_if_needed(self) -> bool:
        """Restore a leftover backup from an abnormally ended run.

        Returns True if a recovery happened. Safe to call on every startup.
        """
        if not self.has_backup:
            return False
        log.warning("found leftover policy backup; restoring institution policy first")
        self.clear()
        return True

    # -- apply / clear -----------------------------------------------------
    def apply(self, patterns: list[str], *, block_incognito: bool = True) -> None:
        """Apply Lab Sentinel's blocklist on top of existing institution policy."""
        clean = _normalize(patterns)
        if len(clean) > MAX_OUR_ENTRIES:
            log.warning(
                "blocklist truncated from %d to %d entries (browser policy limit)",
                len(clean),
                MAX_OUR_ENTRIES,
            )
            clean = clean[:MAX_OUR_ENTRIES]

        # Capture the pristine state once, before touching anything.
        backup = self._load_backup()
        if backup is None:
            backup = self._capture()
            self._write_backup(backup)

        for name, paths in self._browsers.items():
            original = backup["browsers"].get(name, {})
            try:
                self._apply_browser(paths, original, clean, block_incognito)
            except RegistryError as exc:
                # Typically "access denied" when not running elevated.
                log.error("could not apply %s policy: %s", name, exc)

        self._write_marker(len(clean), block_incognito)
        self._state = PolicyState(
            applied=True, patterns=tuple(clean), incognito_blocked=block_incognito
        )
        log.info("browser policy applied: %d blocked patterns", len(clean))

    def clear(self) -> None:
        """Restore the exact original policy state and forget the backup."""
        backup = self._load_backup()
        if backup is None:
            self._state = PolicyState(applied=False)
            return

        for name, paths in self._browsers.items():
            original = backup["browsers"].get(name, {})
            try:
                self._restore_browser(paths, original)
            except RegistryError as exc:
                # Keep the backup so a later attempt (elevated) can still restore.
                log.error("could not restore %s policy: %s", name, exc)
                return

        self._clear_marker()
        self._discard_backup()
        self._state = PolicyState(applied=False)
        log.info("browser policy cleared; institution policy restored")

    # -- per-browser work --------------------------------------------------
    def _apply_browser(
        self,
        paths: dict[str, str],
        original: dict[str, Any],
        patterns: list[str],
        block_incognito: bool,
    ) -> None:
        blocklist_path = paths["blocklist_path"]
        institution = _ordered_entries(original.get("blocklist", {}))

        # Institution entries keep priority 1..N; ours follow.
        merged = institution + [p for p in patterns if p not in institution]

        # Rewrite the key from scratch so stale entries from a previous
        # (larger) ruleset cannot linger.
        self._reg.delete_key(Hive.HKLM, blocklist_path)
        self._reg.create_key(Hive.HKLM, blocklist_path)
        for index, pattern in enumerate(merged, start=1):
            self._reg.write_value(Hive.HKLM, blocklist_path, str(index), pattern, ValueType.SZ)

        if block_incognito:
            self._reg.write_value(
                Hive.HKLM,
                paths["policy_path"],
                paths["incognito_value"],
                INCOGNITO_DISABLED,
                ValueType.DWORD,
            )

    def _restore_browser(self, paths: dict[str, str], original: dict[str, Any]) -> None:
        blocklist_path = paths["blocklist_path"]
        had_blocklist = bool(original.get("blocklist_existed"))
        self._reg.delete_key(Hive.HKLM, blocklist_path)
        if had_blocklist:
            self._reg.create_key(Hive.HKLM, blocklist_path)
            for name, value in original.get("blocklist", {}).items():
                self._reg.write_value(Hive.HKLM, blocklist_path, name, value, ValueType.SZ)

        incognito_name = paths["incognito_value"]
        if original.get("incognito_existed"):
            self._reg.write_value(
                Hive.HKLM,
                paths["policy_path"],
                incognito_name,
                original.get("incognito_value"),
                ValueType.DWORD,
            )
        else:
            self._reg.delete_value(Hive.HKLM, paths["policy_path"], incognito_name)

    # -- backup ------------------------------------------------------------
    def _capture(self) -> dict[str, Any]:
        browsers: dict[str, Any] = {}
        for name, paths in self._browsers.items():
            blocklist_exists = self._reg.key_exists(Hive.HKLM, paths["blocklist_path"])
            blocklist = {
                k: v for k, (v, t) in self._reg.read_values(
                    Hive.HKLM, paths["blocklist_path"]
                ).items() if t is ValueType.SZ
            }
            policy_values = self._reg.read_values(Hive.HKLM, paths["policy_path"])
            incognito = policy_values.get(paths["incognito_value"])
            browsers[name] = {
                "blocklist_existed": blocklist_exists,
                "blocklist": blocklist,
                "incognito_existed": incognito is not None,
                "incognito_value": incognito[0] if incognito is not None else None,
            }
        return {"version": BACKUP_VERSION, "taken_at": self._clock(), "browsers": browsers}

    def _load_backup(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._backup_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError):
            log.error("policy backup is unreadable; refusing to touch browser policy")
            return None
        if not isinstance(data, dict) or data.get("version") != BACKUP_VERSION:
            log.error("policy backup has an unexpected format; ignoring")
            return None
        data.setdefault("browsers", {})
        return data

    def _write_backup(self, backup: dict[str, Any]) -> None:
        self._backup_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._backup_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(backup, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._backup_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _discard_backup(self) -> None:
        with contextlib.suppress(OSError):
            self._backup_path.unlink()

    # -- admin-visible marker ---------------------------------------------
    def _write_marker(self, count: int, block_incognito: bool) -> None:
        try:
            self._reg.write_value(Hive.HKLM, MARKER_PATH, "Managing", 1, ValueType.DWORD)
            self._reg.write_value(
                Hive.HKLM, MARKER_PATH, "BlockedPatternCount", count, ValueType.DWORD
            )
            self._reg.write_value(
                Hive.HKLM, MARKER_PATH, "IncognitoBlocked", int(block_incognito), ValueType.DWORD
            )
            self._reg.write_value(
                Hive.HKLM, MARKER_PATH, "UpdatedAt", _iso(self._clock()), ValueType.SZ
            )
        except RegistryError:
            log.debug("could not write policy marker", exc_info=True)

    def _clear_marker(self) -> None:
        try:
            self._reg.write_value(Hive.HKLM, MARKER_PATH, "Managing", 0, ValueType.DWORD)
            self._reg.write_value(
                Hive.HKLM, MARKER_PATH, "UpdatedAt", _iso(self._clock()), ValueType.SZ
            )
        except RegistryError:
            log.debug("could not clear policy marker", exc_info=True)


def _normalize(patterns: list[str]) -> list[str]:
    """Trim, drop empties, and de-duplicate while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in patterns:
        p = raw.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _ordered_entries(blocklist: dict[str, str]) -> list[str]:
    """Original entries in their numeric order (non-numeric names last)."""

    def sort_key(item: tuple[str, str]) -> tuple[int, int | str]:
        name = item[0]
        return (0, int(name)) if name.isdigit() else (1, name)

    return [v for _, v in sorted(blocklist.items(), key=sort_key)]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
