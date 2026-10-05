"""What the setup window does, without the window.

One blocking call, :meth:`SetupFlow.pair_and_start`, takes the 6-digit code
someone typed and leaves the PC either monitored or with a plain-language
reason why not:

1. check the code looks like a code (and the optional address like an address);
2. stop the agent service if it runs (re-pairing an installed PC);
3. pair (``LabSentinelAgent(pair_only=True)``, the same path as ``--pair-only``);
4. remember a typed dashboard address for the service (agent.toml);
5. install and start the Windows Service.

The window (setup_window.py) only collects input and shows the result, so all
of this is tested without a display.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from labsentinel_agent.app import (
    EXIT_ALREADY_RUNNING,
    EXIT_NO_DASHBOARD,
    EXIT_OK,
    EXIT_PAIRING_REJECTED,
    LabSentinelAgent,
    read_token,
)
from labsentinel_shared.config import AgentConfig

log = logging.getLogger("labsentinel.agent.setup")

SERVICE_NAME = "LabSentinelAgent"


@dataclass(frozen=True)
class SetupResult:
    ok: bool
    title: str
    detail: str
    exit_code: int = 0


def normalize_code(text: str) -> str | None:
    """'482 913', '482-913' -> '482913'. None unless exactly six digits."""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6 or len(text.strip()) > 9:
        return None
    return digits


_ADDRESS = re.compile(r"^(?:wss?://)?[A-Za-z0-9.\-]+(?::\d{1,5})?/?$")


def normalize_address(text: str) -> str | None:
    """'192.168.1.10' -> 'ws://192.168.1.10:8765'. '' -> ''. None if invalid."""
    value = text.strip()
    if not value:
        return ""
    if not _ADDRESS.match(value):
        return None
    if not value.startswith(("ws://", "wss://")):
        value = "ws://" + value
    host_part = value.split("://", 1)[1].rstrip("/")
    if ":" not in host_part:
        value = value.rstrip("/") + ":8765"
    return value.rstrip("/")


# ------------------------------------------------------------- the service
class ServiceControl(Protocol):
    def is_installed(self) -> bool: ...
    def is_running(self) -> bool: ...
    def stop(self) -> None: ...
    def install_and_start(self) -> bool: ...


class WindowsServiceControl:
    """The Lab Sentinel Agent service, through sc.exe and labsentinel-service."""

    def __init__(self, *, wait_s: float = 20.0) -> None:
        self._wait = wait_s

    @staticmethod
    def _service_cmd() -> list[str]:
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).with_name("labsentinel-service.exe"))]
        return [sys.executable, "-m", "labsentinel_agent.service"]

    @staticmethod
    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args, capture_output=True, text=True, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def _query(self) -> str:
        try:
            done = self._run(["sc.exe", "query", SERVICE_NAME])
        except OSError:
            return ""
        return done.stdout if done.returncode == 0 else ""

    def is_installed(self) -> bool:
        return bool(self._query())

    def is_running(self) -> bool:
        return "RUNNING" in self._query()

    def stop(self) -> None:
        # Stopping to re-pair is deliberate: prepare-upgrade waits for the stop
        # and forgets it, so it is not reported as tampering.
        with contextlib.suppress(OSError):
            self._run([*self._service_cmd(), "prepare-upgrade"])

    def install_and_start(self) -> bool:
        try:
            self._run([*self._service_cmd(), "install"])
            self._run(["sc.exe", "start", SERVICE_NAME])
        except OSError:
            log.exception("could not install or start the service")
            return False
        deadline = time.monotonic() + self._wait
        while time.monotonic() < deadline:
            if self.is_running():
                return True
            time.sleep(0.5)
        return False


class NoServiceControl:
    """Off Windows (development): there is no service to manage."""

    def is_installed(self) -> bool:
        return False

    def is_running(self) -> bool:
        return False

    def stop(self) -> None:
        return None

    def install_and_start(self) -> bool:
        return False


def default_service_control() -> ServiceControl:
    return WindowsServiceControl() if sys.platform == "win32" else NoServiceControl()


# ---------------------------------------------------------------- the flow
class SetupFlow:
    def __init__(
        self,
        config: AgentConfig,
        *,
        service: ServiceControl,
        config_file: Path | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._config = config
        self._service = service
        self._config_file = config_file
        self._timeout = timeout_s

    def already_paired(self) -> bool:
        return read_token(self._config) is not None

    def service_running(self) -> bool:
        return self._service.is_running()

    def pair_and_start(self, code_text: str, address_text: str = "") -> SetupResult:
        """Blocking; call from a worker thread, never the UI thread."""
        code = normalize_code(code_text)
        if code is None:
            return SetupResult(False, "Kode belum benar",
                               "Kode pemasangan terdiri dari 6 angka, misalnya 482 913.")
        address = normalize_address(address_text)
        if address is None:
            return SetupResult(False, "Alamat dashboard belum benar",
                               "Isi seperti 192.168.1.10 atau ws://192.168.1.10:8765, "
                               "atau kosongkan agar dicari otomatis.")

        if self._service.is_running():
            log.info("stopping the agent service to pair again")
            self._service.stop()

        config = self._config
        if address:
            config = config.model_copy(update={"direct_url": address})
        agent = LabSentinelAgent(config, pairing_code=code, pair_only=True)
        agent.pair_timeout_s = self._timeout
        try:
            exit_code = asyncio.run(self._pair(agent))
        except Exception as exc:
            log.exception("pairing failed unexpectedly")
            return SetupResult(False, "Terjadi kesalahan",
                               f"Pemasangan tidak selesai ({type(exc).__name__}). "
                               "Coba lagi; bila berulang, hubungi laboran.", 1)

        if exit_code != EXIT_OK:
            reason = agent.client.pairing_rejected if agent.client else None
            return _explain_failure(exit_code, reason or "")

        if address:
            self._remember_address(address)
        started = self._service.install_and_start()
        where = f"dashboard “{agent.dashboard_name}”" if agent.dashboard_name else "dashboard"
        if started:
            detail = (f"PC ini terhubung ke {where}. Pemantauan berjalan otomatis setiap "
                      "kali PC menyala; jendela ini boleh ditutup.")
        elif isinstance(self._service, NoServiceControl):
            detail = (f"PC ini terhubung ke {where}. (Mode pengembangan: jalankan "
                      "labsentinel-agent untuk mulai memantau.)")
        else:
            detail = (f"PC ini terhubung ke {where}, tetapi layanan pemantauan belum "
                      "berjalan. Restart PC, atau hubungi laboran.")
        return SetupResult(True, "Berhasil dipasangkan", detail, EXIT_OK)

    async def _pair(self, agent: LabSentinelAgent) -> int:
        try:
            return await asyncio.wait_for(agent.run(), timeout=self._timeout + 10)
        except TimeoutError:
            return EXIT_NO_DASHBOARD
        finally:
            await agent.shutdown()

    def _remember_address(self, address: str) -> None:
        """Keep a typed address for the service (discovery may not reach it)."""
        path = self._config_file
        if path is None:
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines = [ln for ln in lines if not ln.strip().startswith("direct_url")]
            lines.append(f'direct_url = "{address}"')
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            log.warning("could not save the dashboard address to %s", path, exc_info=True)


def _explain_failure(exit_code: int, reason: str) -> SetupResult:
    if exit_code == EXIT_PAIRING_REJECTED:
        if "salah" in reason:
            return SetupResult(False, "Kode salah",
                               "Periksa kode 6 digit di dashboard: halaman Aturan, kartu "
                               "“Pemasangan PC baru”. Kode mungkin baru saja diganti.", exit_code)
        if "ditutup" in reason:
            return SetupResult(False, "Pendaftaran PC baru sedang ditutup",
                               "Minta pengawas membuka pendaftaran di dashboard (Aturan → "
                               "Pemasangan PC baru), lalu coba lagi.", exit_code)
        if "tunggu" in reason:
            return SetupResult(False, "Terlalu banyak percobaan",
                               f"Terlalu banyak kode salah dari PC ini; {reason}.", exit_code)
        return SetupResult(False, "Pemasangan ditolak",
                           f"Dashboard menolak: {reason or 'tanpa alasan'}.", exit_code)
    if exit_code == EXIT_NO_DASHBOARD:
        return SetupResult(False, "Dashboard tidak ditemukan",
                           "Pastikan dashboard pengawas sedang berjalan dan PC ini berada di "
                           "jaringan lab yang sama. Bila berbeda jaringan, isi alamat dashboard "
                           "di bagian lanjutan.", exit_code)
    if exit_code == EXIT_ALREADY_RUNNING:
        return SetupResult(False, "Agent sedang berjalan",
                           "Ada agent lain yang berjalan di PC ini. Tutup jendela/perintah "
                           "agent tersebut, lalu coba lagi.", exit_code)
    return SetupResult(False, "Pemasangan gagal", f"Kode keluar {exit_code}.", exit_code)
