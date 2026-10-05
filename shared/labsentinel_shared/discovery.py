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
import logging
import socket
from dataclasses import dataclass

SERVICE_ID = "labsentinel"
DISCOVERY_VERSION = 1
DEFAULT_DISCOVERY_PORT = 50123
PROBE_INTERVAL_S = 1.5  # an agent re-asks this often while it looks

log = logging.getLogger("labsentinel.discovery")

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


def _broadcast_socket(*, shared: bool = True) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if shared:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return sock


def _dashboard_socket(port: int) -> socket.socket:
    """The dashboard's socket on the discovery port (sends beacons, gets probes).

    On Windows two sockets sharing a UDP port with SO_REUSEADDR each get
    broadcasts, but a unicast datagram (a probe sent to 127.0.0.1 by an agent
    on the same PC) lands in only one of them, unpredictably. The dashboard
    therefore takes the port exclusively there, so probes always reach it;
    an agent on the same PC then listens on its own probe socket instead.
    If an agent already holds the port, fall back to sharing it.
    """
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:
        sock = _broadcast_socket(shared=False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            sock.bind(("", port))
            return sock
        except OSError as exc:
            log.info("discovery port %s is in use (%s); sharing it", port, exc)
            sock.close()
    sock = _broadcast_socket()
    # Bind to the discovery port to also receive probes.
    try:
        sock.bind(("", port))
    except OSError as exc:
        log.warning("cannot listen for discovery probes on UDP %s: %s", port, exc)
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
        self._sock = _dashboard_socket(self._port)
        self._sock.setblocking(False)
        self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        try:
            await self._serve()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("discovery beacon stopped")

    async def _serve(self) -> None:
        assert self._sock is not None
        loop = asyncio.get_running_loop()
        payload = self._beacon.to_bytes()
        answered: set[str] = set()
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
                    try:
                        self._sock.sendto(payload, addr)
                    except OSError as exc:
                        log.warning("could not answer a discovery probe from %s: %s", addr, exc)
                        continue
                    if addr[0] not in answered:  # once per PC, not every probe
                        answered.add(addr[0])
                        log.info("answered a discovery probe from %s", addr[0])

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
    """Agent-side: probe + listen for a beacon. Returns the first valid one.

    Two sockets: ``probe`` (an ephemeral port of its own) asks every dashboard
    on the LAN - and one on this very PC, via 127.0.0.1 - to answer at once,
    and the answer comes back to that private port; ``listen`` (the discovery
    port, shared) hears the beacons dashboards broadcast every few seconds,
    for networks where probes do not get through.
    """
    loop = asyncio.get_running_loop()
    probe_payload = json.dumps(_PROBE).encode("utf-8")
    probe = _broadcast_socket(shared=False)
    listen: socket.socket | None = None
    try:
        probe.bind(("", 0))
        probe.setblocking(False)

        def send_probes() -> None:
            for target in ("255.255.255.255", "127.0.0.1"):
                with contextlib.suppress(OSError):
                    probe.sendto(probe_payload, (target, port))

        # Probe before taking the shared port, so that a probe to 127.0.0.1
        # cannot be delivered back to our own listener.
        send_probes()
        listen = _broadcast_socket()
        try:
            listen.bind(("", port))
            listen.setblocking(False)
        except OSError:  # held exclusively by a dashboard on this PC
            listen.close()
            listen = None
        log.debug("looking for a dashboard on UDP %s (shared port: %s)", port,
                  "yes" if listen is not None else "no")

        socks = [probe] + ([listen] if listen is not None else [])
        pending = {
            asyncio.ensure_future(loop.sock_recvfrom(s, 2048)): s for s in socks
        }
        deadline = loop.time() + timeout_s
        next_probe = loop.time() + PROBE_INTERVAL_S
        try:
            while (now := loop.time()) < deadline:
                if now >= next_probe:
                    send_probes()
                    next_probe = now + PROBE_INTERVAL_S
                done, _ = await asyncio.wait(
                    pending,
                    timeout=max(0.05, min(deadline, next_probe) - now),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for fut in done:
                    sock = pending.pop(fut)
                    try:
                        data, _ = fut.result()
                    except ConnectionResetError:
                        # Windows reports "port unreachable" for an earlier
                        # probe (no dashboard at 127.0.0.1); keep listening.
                        data = b""
                    except OSError:
                        continue  # this socket is unusable; drop it
                    beacon = parse_beacon(data)
                    if beacon is not None:
                        return beacon
                    pending[asyncio.ensure_future(loop.sock_recvfrom(sock, 2048))] = sock
                if not pending:
                    break
            return None
        finally:
            for fut in pending:
                fut.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    finally:
        probe.close()
        if listen is not None:
            listen.close()
