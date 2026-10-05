"""Run the dashboard with simulated lab PCs, for trying the UI without a lab.

Starts the real dashboard (real WebSocket server, real database) plus a number
of simulated agents that connect, heartbeat, stream a generated screen and
occasionally trip a violation. Everything below the UI is the production code
path — only the agents are fake.

Simulated PCs pair the same way real ones do: on first run each one presents
the dashboard's 6-digit code, receives its own token and keeps it next to the
demo database, so later runs reconnect with that token alone.

    python scripts/demo_lab.py [--pcs 12]

Press Ctrl+C to stop. Data goes to a temporary folder, not your real database.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in ("shared", "agent", "dashboard"):
    sys.path.insert(0, str(_ROOT / _p))

from labsentinel_agent.transport.client import (  # noqa: E402
    AgentIdentity,
    BackoffPolicy,
    CommandResult,
    DashboardClient,
)
from labsentinel_dashboard.app import DashboardApp  # noqa: E402
from labsentinel_shared.config import DashboardConfig  # noqa: E402
from labsentinel_shared.enums import (  # noqa: E402
    AgentState,
    CommandType,
    StreamMode,
    ViolationKind,
)
from labsentinel_shared.protocol import (  # noqa: E402
    CommandPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import RuleSet  # noqa: E402

ROOMS = ["LAB-A", "LAB-B"]
STUDENTS = ["andi", "budi", "citra", "dewi", "eka", "fajar", "gita", "hadi",
            "intan", "joko", "kirana", "lukman", "maya", "nanda", "oka", "putri"]
APPS = ["game.exe", "steam.exe", "discord.exe", "minecraft.exe", "epicgames.exe"]
SITES = [("facebook.com", "Facebook - Google Chrome"),
         ("chat.openai.com", "ChatGPT - Google Chrome"),
         ("roblox.com", "Roblox - Google Chrome")]


def make_frame(label: str, state: str, width: int, height: int) -> bytes:
    """A synthetic 'screen' so the grid shows something recognisable."""
    from PIL import Image, ImageDraw

    hue = (abs(hash(label)) % 360) / 360.0
    import colorsys

    r, g, b = colorsys.hsv_to_rgb(hue, 0.25, 0.22 if state != "paused" else 0.14)
    img = Image.new("RGB", (width, height), (int(r * 255), int(g * 255), int(b * 255)))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width - 1, int(height * 0.09)], fill=(28, 32, 40))
    for i in range(6):
        y = int(height * (0.22 + i * 0.11))
        d.rectangle([int(width * 0.08), y, int(width * (0.35 + 0.5 * random.random())), y + 6],
                    fill=(255, 255, 255, 40))
    d.text((int(width * 0.03), int(height * 0.025)), f"{label}  ·  {state}", fill=(235, 238, 245))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55)
    return buf.getvalue()


class SimulatedPC:
    def __init__(self, index: int, url: str, pairing_code: str, token_dir: Path) -> None:
        self.name = f"{ROOMS[index % len(ROOMS)]}-{index // len(ROOMS) + 1:02d}"
        self.user = STUDENTS[index % len(STUDENTS)]
        self.state = AgentState.ENFORCING
        self.stream = StreamMode.OFF
        self.url = url
        agent_id = f"pc-{index:03d}"
        self._token_file = token_dir / agent_id
        token = None
        with contextlib.suppress(OSError):
            token = self._token_file.read_text(encoding="utf-8").strip() or None
        self.client = DashboardClient(
            identity=AgentIdentity(agent_id=agent_id, hostname=self.name,
                                   username=self.user, agent_version="0.1.0-demo"),
            token=token,
            # Like the real agent: the code is only presented while unpaired.
            pairing_code=None if token else pairing_code,
            on_token_issued=self._store_token,
            state_provider=lambda: self.state,
            metrics_provider=lambda: (random.uniform(0.4, 1.9), random.uniform(38, 62),
                                      random.choice(["Google Chrome", "Visual Studio Code",
                                                     "Microsoft Edge"])),
            on_ruleset=self.on_ruleset,
            on_command=self.on_command,
            heartbeat_interval_s=3.0,
            backoff=BackoffPolicy(base_s=0.5, max_s=3.0, jitter_s=0.2),
        )
        self.ruleset: RuleSet | None = None

    def _store_token(self, token: str) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(token, encoding="utf-8")

    async def on_ruleset(self, rs: RuleSet) -> None:
        self.ruleset = rs

    async def on_command(self, cmd: CommandPayload) -> CommandResult:
        if cmd.command is CommandType.PAUSE:
            self.state = AgentState.PAUSED
        elif cmd.command is CommandType.RESUME:
            self.state = AgentState.ENFORCING
        elif cmd.command is CommandType.SET_STREAM:
            with contextlib.suppress(ValueError):
                self.stream = StreamMode(cmd.text)
        # Simulate one flaky PC that fails to lock.
        if cmd.command is CommandType.LOCK_SCREEN and self.name.endswith("-03"):
            return CommandResult(resulting_state=self.state, ok=False, message="layar sibuk")
        return CommandResult(resulting_state=self.state)

    async def run(self) -> None:
        async def url_provider() -> str:
            return self.url

        # Keep a reference so the connection task is not garbage collected.
        self._link_task = asyncio.ensure_future(self.client.run(url_provider))
        await asyncio.sleep(random.uniform(0.2, 2.0))
        await asyncio.gather(self._stream_loop(), self._violation_loop())

    async def _stream_loop(self) -> None:
        while True:
            if self.stream is StreamMode.OFF or self.state is AgentState.PAUSED:
                await asyncio.sleep(0.4)
                continue
            hi = self.stream is StreamMode.HIRES
            w, h = (1280, 720) if hi else (320, 180)
            jpeg = make_frame(self.name, str(self.state), w, h)
            await self.client.send_event(ScreenFramePayload(
                agent_id=self.client.agent_id, hi_res=hi, width=w, height=h,
                jpeg_b64=base64.b64encode(jpeg).decode(), captured_at=time.time()))
            await asyncio.sleep(0.2 if hi else 1.0)

    async def _violation_loop(self) -> None:
        while True:
            await asyncio.sleep(random.uniform(20, 70))
            if self.state is AgentState.PAUSED or self.ruleset is None:
                continue
            dry = self.ruleset.dry_run
            if random.random() < 0.6:
                app = random.choice(APPS)
                kind = ViolationKind.APP_BLOCKED if dry else ViolationKind.APP_CLOSED
                subject, detail, from_title = app, f"'{app}' tidak ada dalam whitelist", False
            else:
                site, title = random.choice(SITES)
                kind, subject = ViolationKind.SITE_TITLE, site
                detail, from_title = f"Judul jendela: {title}", True
            shot = make_frame(self.name, "pelanggaran", 640, 360)
            await self.client.send_event(ViolationPayload(
                agent_id=self.client.agent_id, kind=kind, subject=subject, detail=detail,
                detected_from_title=from_title, dry_run=dry, occurred_at=time.time(),
                screenshot_b64=base64.b64encode(shot).decode()))


def start_agents(count: int, url: str, pairing_code: str, token_dir: Path) -> threading.Thread:
    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        pcs = [SimulatedPC(i, url, pairing_code, token_dir) for i in range(count)]
        loop.run_until_complete(asyncio.gather(*(p.run() for p in pcs)))

    thread = threading.Thread(target=runner, name="demo-agents", daemon=True)
    thread.start()
    return thread


def main() -> int:
    parser = argparse.ArgumentParser(prog="demo_lab")
    parser.add_argument("--pcs", type=int, default=12, help="Jumlah PC simulasi.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    data_dir = Path(tempfile.gettempdir()) / "labsentinel-demo"
    config = DashboardConfig(
        data_dir=data_dir,
        dashboard_name="Lab Sentinel (demo)",
        ws_host="127.0.0.1",
        ws_port=8765,
        advertise_host="127.0.0.1",
        default_dry_run=True,
    )
    from labsentinel_shared.logging_setup import setup_logging

    setup_logging(log_path=config.log_path, level="WARNING")

    app = DashboardApp(config)
    app.start_background()
    start_agents(
        args.pcs, f"ws://127.0.0.1:{config.ws_port}", app.pairing.code, data_dir / "demo_tokens"
    )
    print(f"\n  {args.pcs} PC simulasi sedang dipasangkan dan terhubung...\n")
    try:
        app.run_ui(open_browser=not args.no_browser, keep_running=True)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
