"""Zero-config dashboard discovery over UDP broadcast (LAN only).

The dashboard periodically broadcasts a small beacon announcing its WebSocket
endpoint. Agents listen for it, so no IP addresses need to be configured by
hand. Agents may also send a one-shot probe to trigger an immediate beacon
reply for fast first contact.

The beacon carries no secrets — only enough for an agent to find the server and
then authenticate over the (optionally TLS) WebSocket with its pairing token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import dataclass

SERVICE_ID = "labsentinel"
DISCOVERY_VERSION = 1
DEFAULT_DISCOVERY_PORT = 50123

_PROBE = {"service": SERVICE_ID, "kind": "probe", "v": DISCOVERY_VERSION}


@dataclass(frozen=True)
class Beacon:
    """A parsed dashboard announcement."""

    ws_host: str
    ws_port: int
    dashboard_name: str
    tls: bool = False

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "service": SERVICE_ID,
                "kind": "beacon",
                "v": DISCOVERY_VERSION,
                "ws_host": self.ws_host,
                "ws_port": self.ws_port,
                "name": self.dashboard_name,
                "tls": self.tls,
            }
        ).encode("utf-8")

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.tls else "ws"
        return f"{scheme}://{self.ws_host}:{self.ws_port}"


def parse_beacon(data: bytes) -> Beacon | None:
    """Parse datagram bytes into a Beacon, or None if it is not one of ours."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("service") != SERVICE_ID or obj.get("kind") != "beacon":
        return None
    if obj.get("v") != DISCOVERY_VERSION:
        return None
    try:
        host = str(obj["ws_host"])
        port = int(obj["ws_port"])
    except (KeyError, TypeError, ValueError):
        return None
    if not host or not (0 < port < 65536):
        return None
    return Beacon(
        ws_host=host,
        ws_port=port,
        dashboard_name=str(obj.get("name", "Dashboard")),
        tls=bool(obj.get("tls", False)),
    )


def is_probe(data: bytes) -> bool:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(obj, dict) and obj.get("service") == SERVICE_ID and obj.get("kind") == "probe"


def _broadcast_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


class DashboardBeaconServer:
    """Broadcasts beacons and answers probes. Runs in the dashboard's loop."""

    def __init__(
        self,
        beacon: Beacon,
        *,
        port: int = DEFAULT_DISCOVERY_PORT,
        interval_s: float = 3.0,
    ) -> None:
        self._beacon = beacon
        self._port = port
        self._interval = interval_s
        self._sock: socket.socket | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop.clear()
        self._sock = _broadcast_socket()
        # Bind to the discovery port to also receive probes.
        with contextlib.suppress(OSError):
            self._sock.bind(("", self._port))
        self._sock.setblocking(False)
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        assert self._sock is not None
        loop = asyncio.get_running_loop()
        payload = self._beacon.to_bytes()
        while not self._stop.is_set():
            with contextlib.suppress(OSError):
                self._sock.sendto(payload, ("255.255.255.255", self._port))
            # Answer any pending probes quickly, then idle until next interval.
            deadline = loop.time() + self._interval
            while loop.time() < deadline and not self._stop.is_set():
                try:
                    data, addr = await asyncio.wait_for(
                        loop.sock_recvfrom(self._sock, 2048), timeout=0.5
                    )
                except (TimeoutError, OSError):
                    continue
                if is_probe(data):
                    with contextlib.suppress(OSError):
                        self._sock.sendto(payload, addr)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            with contextlib.suppress(asyncio.CancelledError):
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        if self._sock:
            self._sock.close()
            self._sock = None


async def discover_dashboard(
    *, port: int = DEFAULT_DISCOVERY_PORT, timeout_s: float = 8.0
) -> Beacon | None:
    """Agent-side: probe + listen for a beacon. Returns the first valid one."""
    loop = asyncio.get_running_loop()
    sock = _broadcast_socket()
    try:
        with contextlib.suppress(OSError):
            sock.bind(("", port))
        sock.setblocking(False)
        # Nudge the server for an immediate reply.
        with contextlib.suppress(OSError):
            sock.sendto(json.dumps(_PROBE).encode("utf-8"), ("255.255.255.255", port))

        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                data, _ = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 2048), timeout=max(0.1, remaining)
                )
            except (TimeoutError, OSError):
                continue
            beacon = parse_beacon(data)
            if beacon is not None:
                return beacon
        return None
    finally:
        sock.close()
