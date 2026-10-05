"""Discovery over real UDP sockets on this machine (dashboard and agent on one PC)."""

from __future__ import annotations

import asyncio
import socket

from labsentinel_shared.discovery import (
    Beacon,
    DashboardBeaconServer,
    discover_dashboard,
)

BEACON = Beacon(ws_host="127.0.0.1", ws_port=8765, dashboard_name="Lab A")


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


async def test_agent_finds_dashboard_on_the_same_pc() -> None:
    port = _free_udp_port()
    # A long interval: only the answer to the agent's probe can arrive in time.
    server = DashboardBeaconServer(BEACON, port=port, interval_s=60)
    await server.start()
    try:
        await asyncio.sleep(0.2)  # let the first broadcast pass
        found = await discover_dashboard(port=port, timeout_s=3)
    finally:
        await server.stop()
    assert found == BEACON


async def test_agent_finds_dashboard_when_it_cannot_share_the_port() -> None:
    """A dashboard holding the port exclusively still answers the 127.0.0.1 probe."""
    port = _free_udp_port()
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # no SO_REUSEADDR
    holder.bind(("", port))
    holder.setblocking(False)
    loop = asyncio.get_running_loop()

    async def answer_probes() -> None:
        while True:
            _, addr = await loop.sock_recvfrom(holder, 2048)
            holder.sendto(BEACON.to_bytes(), addr)

    task = asyncio.ensure_future(answer_probes())
    try:
        found = await discover_dashboard(port=port, timeout_s=3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        holder.close()
    assert found == BEACON


async def test_no_dashboard_gives_up_after_the_timeout() -> None:
    port = _free_udp_port()
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await discover_dashboard(port=port, timeout_s=0.5) is None
    assert loop.time() - start < 2.5
