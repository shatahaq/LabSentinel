"""Open a WebSocket to the dashboard and show what it answers (CI diagnostics).

    python packaging/ci/ws_probe.py ws://127.0.0.1:8765

Sends a HELLO with no credentials; a healthy dashboard answers with a REJECT.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

import websockets


async def main(url: str) -> None:
    async with websockets.connect(url, proxy=None, open_timeout=10) as ws:
        hello = {"v": 1, "id": uuid.uuid4().hex, "ts": time.time(), "token": None,
                 "payload": {"type": "hello", "agent_id": "probe", "hostname": "PROBE"}}
        await ws.send(json.dumps(hello))
        print("dashboard answered:", await asyncio.wait_for(ws.recv(), timeout=10))


asyncio.run(main(sys.argv[1]))
