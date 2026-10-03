"""Registry access port + a Windows implementation and an in-memory fake.

Wrapping the registry lets the browser-policy logic be exercised exhaustively in
tests (including backup/restore correctness) without touching a real machine,
and keeps the Windows-only ``winreg`` import out of the pure logic.
"""

from __future__ import annotations

import logging
import sys
from enum import StrEnum
from typing import Any, Protocol

log = logging.getLogger("labsentinel.agent.registry")


class Hive(StrEnum):
    HKLM = "HKLM"
    HKCU = "HKCU"


class ValueType(StrEnum):
    SZ = "sz"  # string
    DWORD = "dword"  # 32-bit int


class RegistryError(OSError):
    """Raised when a registry operation fails (e.g. access denied)."""


class RegistryPort(Protocol):
    """Minimal registry surface needed for browser policies."""

    def key_exists(self, hive: Hive, path: str) -> bool: ...

    def read_values(self, hive: Hive, path: str) -> dict[str, tuple[Any, ValueType]]:
        """All values under a key as name -> (value, type). Empty if absent."""
        ...

    def write_value(
        self, hive: Hive, path: str, name: str, value: Any, vtype: ValueType
    ) -> None: ...

    def delete_value(self, hive: Hive, path: str, name: str) -> None: ...

    def delete_key(self, hive: Hive, path: str) -> None:
        """Delete a key and its values. No-op if absent."""
        ...

    def create_key(self, hive: Hive, path: str) -> None: ...


class FakeRegistry:
    """In-memory registry for tests. Can simulate access-denied on a path."""

    def __init__(self, *, readonly_paths: frozenset[str] = frozenset()) -> None:
        self._data: dict[tuple[str, str], dict[str, tuple[Any, ValueType]]] = {}
        self._readonly = readonly_paths

    def _guard(self, path: str) -> None:
        if any(path.lower().startswith(p.lower()) for p in self._readonly):
            raise RegistryError(f"access denied: {path}")

    def key_exists(self, hive: Hive, path: str) -> bool:
        return (str(hive), path) in self._data

    def read_values(self, hive: Hive, path: str) -> dict[str, tuple[Any, ValueType]]:
        return dict(self._data.get((str(hive), path), {}))

    def write_value(self, hive: Hive, path: str, name: str, value: Any, vtype: ValueType) -> None:
        self._guard(path)
        self._data.setdefault((str(hive), path), {})[name] = (value, vtype)

    def delete_value(self, hive: Hive, path: str, name: str) -> None:
        self._guard(path)
        self._data.get((str(hive), path), {}).pop(name, None)

    def delete_key(self, hive: Hive, path: str) -> None:
        self._guard(path)
        self._data.pop((str(hive), path), None)

    def create_key(self, hive: Hive, path: str) -> None:
        self._guard(path)
        self._data.setdefault((str(hive), path), {})

    # test helper
    def dump(self) -> dict[tuple[str, str], dict[str, tuple[Any, ValueType]]]:
        return {k: dict(v) for k, v in self._data.items()}


class WinRegistry:
    """Real Windows registry access via ``winreg`` (needs admin for HKLM)."""

    def __init__(self) -> None:
        if sys.platform != "win32":  # pragma: no cover - guard for dev machines
            raise RegistryError("WinRegistry is only available on Windows")
        import winreg

        self._winreg = winreg
        self._hives = {Hive.HKLM: winreg.HKEY_LOCAL_MACHINE, Hive.HKCU: winreg.HKEY_CURRENT_USER}
        self._types = {ValueType.SZ: winreg.REG_SZ, ValueType.DWORD: winreg.REG_DWORD}
        self._rtypes = {winreg.REG_SZ: ValueType.SZ, winreg.REG_DWORD: ValueType.DWORD}

    def key_exists(self, hive: Hive, path: str) -> bool:
        try:
            with self._winreg.OpenKey(self._hives[hive], path):
                return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RegistryError(str(exc)) from exc

    def read_values(self, hive: Hive, path: str) -> dict[str, tuple[Any, ValueType]]:
        out: dict[str, tuple[Any, ValueType]] = {}
        try:
            with self._winreg.OpenKey(self._hives[hive], path) as key:
                count = self._winreg.QueryInfoKey(key)[1]
                for i in range(count):
                    name, value, rtype = self._winreg.EnumValue(key, i)
                    vtype = self._rtypes.get(rtype)
                    if vtype is None:
                        continue  # unsupported type: leave it strictly alone
                    out[name] = (value, vtype)
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RegistryError(str(exc)) from exc
        return out

    def write_value(self, hive: Hive, path: str, name: str, value: Any, vtype: ValueType) -> None:
        try:
            with self._winreg.CreateKeyEx(
                self._hives[hive], path, 0, self._winreg.KEY_SET_VALUE
            ) as key:
                self._winreg.SetValueEx(key, name, 0, self._types[vtype], value)
        except OSError as exc:
            raise RegistryError(f"write {path}\\{name}: {exc}") from exc

    def delete_value(self, hive: Hive, path: str, name: str) -> None:
        try:
            with self._winreg.OpenKey(
                self._hives[hive], path, 0, self._winreg.KEY_SET_VALUE
            ) as key:
                self._winreg.DeleteValue(key, name)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RegistryError(f"delete {path}\\{name}: {exc}") from exc

    def delete_key(self, hive: Hive, path: str) -> None:
        try:
            self._winreg.DeleteKey(self._hives[hive], path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RegistryError(f"delete key {path}: {exc}") from exc

    def create_key(self, hive: Hive, path: str) -> None:
        try:
            self._winreg.CreateKeyEx(self._hives[hive], path, 0, self._winreg.KEY_SET_VALUE).Close()
        except OSError as exc:
            raise RegistryError(f"create key {path}: {exc}") from exc
