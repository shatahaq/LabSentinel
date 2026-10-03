"""Manual tray demo — watch the participant's indicator change states.

The tray icon cannot be verified automatically, so this script drives it by hand.

Run TWO terminals from the repo root:

    1) python scripts/tray_demo.py            # the "service" half
    2) python -m labsentinel_agent.tray --data-dir <dir printed by 1>

Then watch the system tray: the icon and its tooltip cycle through
Aktif -> Dijeda -> Mode uji coba -> Terputus, and two notifications appear.
Hover the icon to read the state in words; right-click to see the read-only menu.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in ("shared", "agent", "dashboard"):
    sys.path.insert(0, str(_ROOT / _p))

from labsentinel_agent.tray.bridge import TrayBridge  # noqa: E402
from labsentinel_shared.enums import AgentState  # noqa: E402

STEPS: list[tuple[str, AgentState, bool]] = [
    ("Pemantauan aktif (hijau)", AgentState.ENFORCING, True),
    ("Pemantauan dijeda (kuning)", AgentState.PAUSED, True),
    ("Mode uji coba / dry-run", AgentState.DRY_RUN, True),
    ("Terputus dari pengawas (merah)", AgentState.ENFORCING, False),
    ("Kembali aktif", AgentState.ENFORCING, True),
]


async def main() -> None:
    data_dir = Path(tempfile.gettempdir()) / "labsentinel-tray-demo"
    data_dir.mkdir(parents=True, exist_ok=True)

    bridge = TrayBridge(
        token_file=data_dir / "tray_token",
        endpoint_file=data_dir / "tray_endpoint",
        port=0,
        dashboard_name="Lab Sentinel (demo)",
    )
    await bridge.start()
    bridge.set_status(AgentState.ENFORCING, connected=True)

    print("=" * 68)
    print("Tray demo berjalan.")
    print(f"  data-dir : {data_dir}")
    print(f"  port IPC : {bridge.bound_port}")
    print()
    print("Di terminal KEDUA, jalankan:")
    print(f'  python -m labsentinel_agent.tray --data-dir "{data_dir}"')
    print("=" * 68)
    print("\nMenunggu helper tray terhubung...")

    for _ in range(600):  # up to 60s
        if bridge.indicator_visible:
            break
        await asyncio.sleep(0.1)
    else:
        print("Helper tray tidak terhubung. Pastikan perintah di atas sudah dijalankan.")
        await bridge.stop()
        return

    print("Helper terhubung. Ikon tray sekarang terlihat.\n")

    for label, state, connected in STEPS:
        print(f"  -> {label}")
        bridge.set_status(state, connected=connected)
        await asyncio.sleep(4)

    print("\n  -> Peringatan aplikasi akan ditutup")
    bridge.warn_closing("game.exe", 5, "tidak ada dalam daftar aplikasi yang diizinkan")
    await asyncio.sleep(4)

    print("  -> Pesan broadcast dari pengawas")
    bridge.notify("Pesan dari pengawas", "Ujian dimulai 5 menit lagi.")
    await asyncio.sleep(4)

    print("\nSelesai. Hentikan helper dengan Ctrl+C di terminal kedua.")
    await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ndihentikan.")
