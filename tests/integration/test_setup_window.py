"""The setup window's flow: code in, PC paired and monitored (or a plain reason).

The flow runs against a real dashboard server; only the Windows Service
manager is replaced. The window itself is driven at the end when tkinter and
a display are available (Windows CI, or Linux with Xvfb).
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

import labsentinel_agent.app as app_mod
from labsentinel_agent.gui.setup_flow import SetupFlow, normalize_address, normalize_code
from labsentinel_agent.policy.registry import FakeRegistry
from labsentinel_dashboard.domain.pairing import PairingService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import AgentSession, WebSocketServer
from labsentinel_shared.config import AgentConfig
from labsentinel_shared.rules import RuleSet

pytestmark = pytest.mark.integration


class FakeService:
    def __init__(self, *, running: bool = False, starts: bool = True) -> None:
        self.running = running
        self.starts = starts
        self.calls: list[str] = []

    def is_installed(self) -> bool:
        return self.running

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.calls.append("stop")
        self.running = False

    def install_and_start(self) -> bool:
        self.calls.append("install_and_start")
        self.running = self.starts
        return self.starts


class Events:
    async def on_agent_connected(self, session: AgentSession) -> None: ...
    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None: ...
    async def on_heartbeat(self, agent_id: str, hb: object) -> None: ...
    async def on_violation(self, agent_id: str, v: object) -> None: ...
    async def on_screen_frame(self, agent_id: str, f: object) -> None: ...
    async def on_ack(self, agent_id: str, ack: object) -> None: ...


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_mod, "_registry", FakeRegistry)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "dash.sqlite")
    yield d
    d.close()


@pytest.fixture
async def dashboard(db: Database) -> AsyncIterator[tuple[str, PairingService]]:
    pairing = PairingService(db)
    srv = WebSocketServer(host="127.0.0.1", port=0, authenticator=pairing,
                          ruleset_provider=lambda h: RuleSet(revision=1), events=Events())
    await srv.start()
    yield f"ws://127.0.0.1:{srv.bound_port}", pairing
    await srv.stop()


def _config(tmp_path: Path, url: str | None = None) -> AgentConfig:
    data = tmp_path / "agent"
    return AgentConfig(data_dir=data, token_file=data / "token", direct_url=url,
                       discovery_timeout_s=0.5)


async def _pair(flow: SetupFlow, code: str, address: str = "") -> object:
    import asyncio

    # The window calls this from a worker thread; so do we.
    return await asyncio.to_thread(flow.pair_and_start, code, address)


# ----------------------------------------------------------- input checks
@pytest.mark.parametrize(("typed", "code"), [
    ("482913", "482913"), ("482 913", "482913"), (" 482-913 ", "482913"),
    ("48291", None), ("4829130", None), ("", None), ("abcdef", None),
])
def test_code_input(typed: str, code: str | None) -> None:
    assert normalize_code(typed) == code


@pytest.mark.parametrize(("typed", "address"), [
    ("", ""), ("192.168.1.10", "ws://192.168.1.10:8765"),
    ("192.168.1.10:9000", "ws://192.168.1.10:9000"),
    ("ws://lab-a.local:8765/", "ws://lab-a.local:8765"),
    ("http://x", None), ("1.2.3.4; rm -rf", None),
])
def test_address_input(typed: str, address: str | None) -> None:
    assert normalize_address(typed) == address


# ------------------------------------------------------------------ flow
async def test_right_code_pairs_and_starts_monitoring(
    dashboard: tuple[str, PairingService], tmp_path: Path, db: Database
) -> None:
    url, pairing = dashboard
    service = FakeService()
    flow = SetupFlow(_config(tmp_path, url), service=service, timeout_s=10)
    assert not flow.already_paired()
    code = pairing.code
    result = await _pair(flow, f"{code[:3]} {code[3:]}")
    assert result.ok, result  # type: ignore[attr-defined]
    assert "Pemantauan berjalan otomatis" in result.detail  # type: ignore[attr-defined]
    assert service.calls == ["install_and_start"]
    assert flow.already_paired()
    assert db.paired_agent_ids()


async def test_wrong_code_explains_where_to_look(
    dashboard: tuple[str, PairingService], tmp_path: Path
) -> None:
    url, pairing = dashboard
    service = FakeService()
    wrong = "100000" if pairing.code != "100000" else "100001"
    flow = SetupFlow(_config(tmp_path, url), service=service, timeout_s=10)
    result = await _pair(flow, wrong)
    assert not result.ok  # type: ignore[attr-defined]
    assert result.title == "Kode salah"  # type: ignore[attr-defined]
    assert "Pemasangan PC baru" in result.detail  # type: ignore[attr-defined]
    assert service.calls == []  # nothing installed for a failed pairing
    # ...and the admin can simply try again with the right code.
    assert (await _pair(flow, pairing.code)).ok  # type: ignore[attr-defined]


async def test_closed_registration_is_explained(
    dashboard: tuple[str, PairingService], tmp_path: Path
) -> None:
    url, pairing = dashboard
    pairing.set_open(False, actor="pengawas")
    flow = SetupFlow(_config(tmp_path, url), service=FakeService(), timeout_s=10)
    result = await _pair(flow, pairing.code)
    assert result.title == "Pendaftaran PC baru sedang ditutup"  # type: ignore[attr-defined]


async def test_no_dashboard_is_explained(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"ws://127.0.0.1:{s.getsockname()[1]}"
    flow = SetupFlow(_config(tmp_path, dead), service=FakeService(), timeout_s=1.5)
    result = await _pair(flow, "123456")
    assert result.title == "Dashboard tidak ditemukan"  # type: ignore[attr-defined]


async def test_bad_input_never_reaches_the_network(tmp_path: Path) -> None:
    flow = SetupFlow(_config(tmp_path), service=FakeService(), timeout_s=1)
    assert (await _pair(flow, "12345")).title == "Kode belum benar"  # type: ignore[attr-defined]
    bad = await _pair(flow, "123456", "http://nope")
    assert bad.title == "Alamat dashboard belum benar"  # type: ignore[attr-defined]


async def test_repairing_an_installed_pc_stops_the_service_first(
    dashboard: tuple[str, PairingService], tmp_path: Path
) -> None:
    url, pairing = dashboard
    service = FakeService(running=True)
    flow = SetupFlow(_config(tmp_path, url), service=service, timeout_s=10)
    assert (await _pair(flow, pairing.code)).ok  # type: ignore[attr-defined]
    assert service.calls == ["stop", "install_and_start"]


async def test_typed_address_is_kept_for_the_service(
    dashboard: tuple[str, PairingService], tmp_path: Path
) -> None:
    url, pairing = dashboard
    toml = tmp_path / "agent.toml"
    toml.write_text('log_level = "INFO"\ndirect_url = "ws://old:1"\n', encoding="utf-8")
    flow = SetupFlow(_config(tmp_path), service=FakeService(), config_file=toml, timeout_s=10)
    assert (await _pair(flow, pairing.code, url.removeprefix("ws://"))).ok  # type: ignore[attr-defined]
    text = toml.read_text(encoding="utf-8")
    assert f'direct_url = "{url}"' in text
    assert "ws://old:1" not in text and 'log_level = "INFO"' in text


async def test_service_that_does_not_start_is_said(
    dashboard: tuple[str, PairingService], tmp_path: Path
) -> None:
    url, pairing = dashboard
    flow = SetupFlow(_config(tmp_path, url), service=FakeService(starts=False), timeout_s=10)
    result = await _pair(flow, pairing.code)
    assert result.ok and "belum berjalan" in result.detail  # type: ignore[attr-defined]


# ---------------------------------------------------------------- window
def _display_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return sys.platform == "win32" or bool(os.environ.get("DISPLAY"))


@pytest.mark.skipif(not _display_available(), reason="needs tkinter and a display")
async def test_window_pairs_from_typed_code(
    dashboard: tuple[str, PairingService], tmp_path: Path
) -> None:
    import asyncio
    import tkinter as tk

    from labsentinel_agent.gui.setup_window import SetupWindow

    url, pairing = dashboard
    root = tk.Tk()
    try:
        win = SetupWindow(root, SetupFlow(_config(tmp_path, url), service=FakeService(),
                                          timeout_s=10))
        assert str(win.primary["state"]) == "disabled"  # nothing typed yet
        win.code_var.set(pairing.code)
        root.update()
        assert str(win.primary["state"]) != "disabled"
        win.submit()
        for _ in range(200):  # the dashboard runs on this test's loop: keep it turning
            root.update()
            if win.state != "busy":
                break
            await asyncio.sleep(0.05)
        assert win.state == "done", win.status_title.cget("text")
        assert "Berhasil" in win.status_title.cget("text")
    finally:
        root.destroy()


@pytest.mark.skipif(not _display_available(), reason="needs tkinter and a display")
def test_alt_a_opens_the_address_field(tmp_path: Path) -> None:
    import tkinter as tk

    from labsentinel_agent.gui.setup_window import SetupWindow

    root = tk.Tk()
    try:
        win = SetupWindow(root, SetupFlow(_config(tmp_path, "ws://127.0.0.1:1"),
                                          service=FakeService()))
        root.update()
        assert not win.address_box.winfo_ismapped()
        win.code.focus_force()
        root.update()
        win.code.event_generate("<Alt-KeyPress-a>", when="now")
        root.update()
        assert win.address_box.winfo_ismapped()
        assert root.focus_get() is win.address
        win.code.event_generate("<Alt-KeyPress-a>", when="now")  # stays open
        root.update()
        assert win.address_box.winfo_ismapped()
    finally:
        root.destroy()
