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
STUDENTS = [
    "andi",
    "budi",
    "citra",
    "dewi",
    "eka",
    "fajar",
    "gita",
    "hadi",
    "intan",
    "joko",
    "kirana",
    "lukman",
    "maya",
    "nanda",
    "oka",
    "putri",
]
APPS = ["game.exe", "steam.exe", "discord.exe", "minecraft.exe", "epicgames.exe"]
SITES = [
    ("facebook.com", "Facebook - Google Chrome"),
    ("chat.openai.com", "ChatGPT - Google Chrome"),
    ("roblox.com", "Roblox - Google Chrome"),
]


# What each simulated participant has open: a code editor or a browser page,
# drawn like a Windows 11 desktop so the dashboard shows believable screens.
SCENES = ["code", "browser", "code", "docs", "browser", "code", "docs", "code"]
CODE = [
    [("kw", "def "), ("fn", "hitung_rata"), ("tx", "(nilai):")],
    [("tx", "    total = "), ("num", "0")],
    [("kw", "    for "), ("tx", "n "), ("kw", "in "), ("tx", "nilai:")],
    [("tx", "        total += n")],
    [("kw", "    return "), ("tx", "total / "), ("fn", "len"), ("tx", "(nilai)")],
    [],
    [("cm", "# Soal 2: urutkan data mahasiswa")],
    [("kw", "def "), ("fn", "urutkan"), ("tx", "(data):")],
    [
        ("kw", "    return "),
        ("fn", "sorted"),
        ("tx", "(data, key="),
        ("kw", "lambda "),
        ("tx", "m: m.nim)"),
    ],
    [],
    [
        ("tx", "data = ["),
        ("st", "'2301'"),
        ("tx", ", "),
        ("st", "'2307'"),
        ("tx", ", "),
        ("st", "'2299'"),
        ("tx", "]"),
    ],
    [("fn", "print"), ("tx", "(urutkan(data))")],
]
CODE_COLORS = {
    "kw": (197, 134, 192),
    "fn": (220, 220, 170),
    "tx": (212, 212, 212),
    "num": (181, 206, 168),
    "st": (206, 145, 120),
    "cm": (106, 153, 85),
}


def _font(px: float, mono: bool = False):
    from PIL import ImageFont

    size = max(6, round(px))
    if mono:  # Consolas on Windows, DejaVu on Linux; else Pillow's own face
        for name in ("consola.ttf", "DejaVuSansMono.ttf"):
            with contextlib.suppress(OSError):
                return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def make_frame(
    label: str,
    state: str,
    width: int,
    height: int,
    *,
    scene: str | None = None,
    url: str | None = None,
) -> bytes:
    """A synthetic Windows desktop for one PC: wallpaper, taskbar and an app.

    ``scene`` picks the open app ("code", "browser" or "docs"); ``url`` puts a
    site in the browser, which is how violation screenshots show the page.
    """
    from PIL import Image, ImageDraw

    u = width / 1280  # every size below is in 1280-wide units
    index = sum(ord(c) for c in label)
    scene = scene or SCENES[index % len(SCENES)]
    now = time.localtime()

    img = Image.new("RGB", (width, height), (0, 0, 0))
    d = ImageDraw.Draw(img)
    # Wallpaper: the soft blue of the default Windows 11 background.
    for y in range(0, height, 4):
        t = y / height
        d.rectangle(
            [0, y, width, y + 4], fill=(int(30 + 40 * t), int(70 + 50 * t), int(140 + 50 * t))
        )

    # Taskbar.
    bar = int(48 * u)
    d.rectangle([0, height - bar, width, height], fill=(32, 32, 36))
    cx = width // 2
    for i, colour in enumerate(
        [(0, 120, 212), (90, 90, 96), (240, 180, 40), (0, 120, 212), (30, 140, 90)]
    ):
        x = cx + int((i - 2) * 44 * u)
        d.rounded_rectangle(
            [x - int(14 * u), height - bar + int(12 * u), x + int(14 * u), height - int(12 * u)],
            radius=int(4 * u),
            fill=colour,
        )
    small = _font(12 * u)
    for row, (text, colour) in enumerate(
        [
            (time.strftime("%H.%M", now), (240, 240, 240)),
            (time.strftime("%d/%m/%Y", now), (200, 200, 200)),
        ]
    ):
        x = width - int(16 * u) - int(d.textlength(text, font=small))
        d.text((x, height - bar + int((9 + row * 16) * u)), text, font=small, fill=colour)

    # The app window, nearly maximised.
    wx0, wy0, wx1, wy1 = int(24 * u), int(18 * u), width - int(24 * u), height - bar - int(14 * u)
    title_h = int(32 * u)
    dark = scene == "code"
    d.rectangle([wx0, wy0, wx1, wy1], fill=(30, 30, 30) if dark else (255, 255, 255))
    d.rectangle([wx0, wy0, wx1, wy0 + title_h], fill=(50, 50, 52) if dark else (238, 240, 243))
    title_font = _font(13 * u)
    titles = {
        "code": f"latihan_{index % 7 + 1}.py - Visual Studio Code",
        "browser": f"{(url or 'elearning.kampus.ac.id')} - Google Chrome",
        "docs": "Laporan Praktikum.docx - Word",
    }
    d.text(
        (wx0 + int(14 * u), wy0 + int(8 * u)),
        titles[scene],
        font=title_font,
        fill=(220, 220, 220) if dark else (40, 40, 40),
    )
    ink = (200, 200, 200) if dark else (60, 60, 60)
    my, g = wy0 + title_h // 2, max(2, int(5 * u))
    close_x, max_x, min_x = (wx1 - int(k * 46 * u) - int(23 * u) for k in range(3))
    d.line([close_x - g, my - g, close_x + g, my + g], fill=ink, width=max(1, int(u)))
    d.line([close_x - g, my + g, close_x + g, my - g], fill=ink, width=max(1, int(u)))
    d.rectangle([max_x - g, my - g, max_x + g, my + g], outline=ink, width=max(1, int(u)))
    d.line([min_x - g, my, min_x + g, my], fill=ink, width=max(1, int(u)))
    top = wy0 + title_h
    body = _font(15 * u)
    line_h = int(24 * u)

    if scene == "code":
        body = _font(14 * u, mono=True)
        d.rectangle([wx0, top, wx0 + int(48 * u), wy1], fill=(51, 51, 51))  # activity bar
        d.rectangle(
            [wx0 + int(48 * u), top, wx0 + int(260 * u), wy1], fill=(37, 37, 38)
        )  # explorer
        for i, name in enumerate(["latihan_1.py", "latihan_2.py", "data.csv", "README.md"]):
            d.text(
                (wx0 + int(70 * u), top + int((14 + i * 26) * u)),
                name,
                font=small,
                fill=(204, 204, 204),
            )
        x0 = wx0 + int(300 * u)
        typed = 6 + int(time.time() / 2) % (len(CODE) - 5)  # the participant keeps typing
        for i, tokens in enumerate(CODE[:typed]):
            y = top + int(20 * u) + i * line_h
            d.text((x0 - int(30 * u), y), str(i + 1), font=body, fill=(110, 118, 129))
            x = x0
            for kind, text in tokens:
                d.text((x, y), text, font=body, fill=CODE_COLORS[kind])
                x += int(d.textlength(text, font=body))
        d.rectangle([wx0, wy1 - int(22 * u), wx1, wy1], fill=(0, 122, 204))  # status bar
    elif scene == "browser":
        d.rectangle([wx0, top, wx1, top + int(40 * u)], fill=(247, 248, 250))
        d.rounded_rectangle(
            [wx0 + int(110 * u), top + int(7 * u), wx1 - int(120 * u), top + int(33 * u)],
            radius=int(13 * u),
            fill=(232, 234, 237),
        )
        d.text(
            (wx0 + int(130 * u), top + int(12 * u)),
            url or "elearning.kampus.ac.id/course/algoritma",
            font=small,
            fill=(60, 64, 67),
        )
        page = top + int(40 * u)
        brand = {
            "facebook.com": (24, 119, 242),
            "chat.openai.com": (16, 163, 127),
            "roblox.com": (226, 35, 26),
        }.get(url or "", (0, 84, 147))
        d.rectangle([wx0, page, wx1, page + int(64 * u)], fill=brand)
        d.text(
            (wx0 + int(40 * u), page + int(20 * u)),
            (url or "E-Learning Kampus").split("/")[0],
            font=_font(20 * u),
            fill=(255, 255, 255),
        )
        for i in range(7):
            y = page + int((100 + i * 34) * u)
            d.rectangle(
                [
                    wx0 + int(40 * u),
                    y,
                    wx0 + int((260 + (index * 37 + i * 91) % 520) * u),
                    y + int(12 * u),
                ],
                fill=(210, 214, 220),
            )
        d.rectangle(
            [wx1 - int(340 * u), page + int(100 * u), wx1 - int(40 * u), page + int(300 * u)],
            fill=(240, 242, 245),
            outline=(220, 223, 228),
        )
    else:  # docs
        d.rectangle([wx0, top, wx1, top + int(88 * u)], fill=(243, 242, 241))  # ribbon
        d.rectangle([wx0, top, wx1, top + int(22 * u)], fill=(43, 87, 154))
        page_x0, page_x1 = cx - int(300 * u), cx + int(300 * u)
        d.rectangle([wx0, top + int(88 * u), wx1, wy1], fill=(230, 230, 230))
        d.rectangle([page_x0, top + int(108 * u), page_x1, wy1], fill=(255, 255, 255))
        d.text(
            (page_x0 + int(50 * u), top + int(140 * u)),
            "Laporan Praktikum Basis Data",
            font=_font(20 * u),
            fill=(30, 30, 30),
        )
        for i in range(9):
            y = top + int((190 + i * 26) * u)
            d.rectangle(
                [
                    page_x0 + int(50 * u),
                    y,
                    page_x1 - int((50 + (index * 13 + i * 47) % 160) * u),
                    y + int(9 * u),
                ],
                fill=(150, 150, 150),
            )

    if state == "paused":
        img = Image.eval(img, lambda v: int(v * 0.55))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
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
            identity=AgentIdentity(
                agent_id=agent_id,
                hostname=self.name,
                username=self.user,
                agent_version="0.1.0-demo",
            ),
            token=token,
            # Like the real agent: the code is only presented while unpaired.
            pairing_code=None if token else pairing_code,
            on_token_issued=self._store_token,
            # One PC shows how a lost desktop worker looks on the dashboard.
            desktop_access_provider=lambda: index != 6,
            input_frozen_provider=lambda: self.frozen,
            state_provider=lambda: self.state,
            metrics_provider=lambda: (
                random.uniform(0.4, 1.9),
                random.uniform(38, 62),
                random.choice(["Google Chrome", "Visual Studio Code", "Microsoft Edge"]),
            ),
            on_ruleset=self.on_ruleset,
            on_command=self.on_command,
            heartbeat_interval_s=3.0,
            backoff=BackoffPolicy(base_s=0.5, max_s=3.0, jitter_s=0.2),
        )
        self.ruleset: RuleSet | None = None
        self.frozen = False

    def _store_token(self, token: str) -> None:
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(token, encoding="utf-8")

    async def on_ruleset(self, rs: RuleSet) -> None:
        self.ruleset = rs

    async def on_command(self, cmd: CommandPayload) -> CommandResult:
        if cmd.command is CommandType.PAUSE:
            self.state = AgentState.PAUSED
            self.frozen = False  # like the real agent: pausing releases a freeze
        elif cmd.command is CommandType.FREEZE_INPUT:
            self.frozen = cmd.text.strip().lower() != "off"
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
            await self.client.send_event(
                ScreenFramePayload(
                    agent_id=self.client.agent_id,
                    hi_res=hi,
                    width=w,
                    height=h,
                    jpeg_b64=base64.b64encode(jpeg).decode(),
                    captured_at=time.time(),
                )
            )
            await asyncio.sleep(0.1 if hi else 0.2)

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
            shot = make_frame(
                self.name,
                "pelanggaran",
                640,
                360,
                scene="browser" if from_title else None,
                url=subject if from_title else None,
            )
            await self.client.send_event(
                ViolationPayload(
                    agent_id=self.client.agent_id,
                    kind=kind,
                    subject=subject,
                    detail=detail,
                    detected_from_title=from_title,
                    dry_run=dry,
                    occurred_at=time.time(),
                    screenshot_b64=base64.b64encode(shot).decode(),
                )
            )


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
