"""Behaviour that only matters in an installed (frozen) build, simulated here."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import labsentinel_agent.app as agent_app
import labsentinel_dashboard.app as dash_app


def _freeze(monkeypatch: pytest.MonkeyPatch, exe_dir: Path, name: str) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / name))


def test_config_next_to_the_installed_exe_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, tmp_path, "labsentinel-agent.exe")
    assert agent_app.default_config_path() is None  # nothing there yet
    (tmp_path / "agent.toml").write_text('direct_url = "ws://10.0.0.5:8765"\n')
    assert agent_app.default_config_path() == tmp_path / "agent.toml"

    _freeze(monkeypatch, tmp_path, "labsentinel-dashboard.exe")
    (tmp_path / "dashboard.toml").write_text("")
    assert dash_app.default_config_path() == tmp_path / "dashboard.toml"


def test_source_checkout_ignores_toml_next_to_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert agent_app.default_config_path() is None
    assert dash_app.default_config_path() is None


def test_web_assets_found_inside_a_frozen_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert dash_app._web_dir() == tmp_path / "web"
    monkeypatch.delattr(sys, "_MEIPASS")
    assert (dash_app._web_dir() / "index.html").exists()


@pytest.mark.parametrize(
    ("chrome", "platform", "expected"),
    [("C:/chrome.exe", "win32", "chrome"), (None, "win32", "edge"), (None, "linux", "default")],
)
def test_admin_window_falls_back_when_chrome_is_missing(
    monkeypatch: pytest.MonkeyPatch, chrome: str | None, platform: str, expected: str
) -> None:
    import eel.chrome

    monkeypatch.setattr(eel.chrome, "find_path", lambda: chrome)
    monkeypatch.setattr(sys, "platform", platform)
    assert dash_app.pick_browser_mode() == expected
