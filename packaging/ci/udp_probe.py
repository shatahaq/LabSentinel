"""Ask for the dashboard over UDP discovery and show who answers (CI diagnostics).

    python packaging/ci/udp_probe.py [PORT]

Sends the agent's probe from a private port to 127.0.0.1, to the LAN broadcast
address and to this PC's own address, then prints every answer for a few
seconds. Prints nothing useful if no dashboard answers; never fails the step.
"""

from __future__ import annotations

import json
import socket
import sys
import time

PROBE = json.dumps({"service": "labsentinel", "kind": "probe", "v": 1}).encode()


def own_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return str(s.getsockname()[0])
        except OSError:
            return "127.0.0.1"


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 50123
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    sock.settimeout(0.5)
    print("probing from", sock.getsockname(), flush=True)
    targets = ["127.0.0.1", "255.255.255.255", own_address()]
    deadline = time.monotonic() + 6
    next_send = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_send:
            for t in targets:
                try:
                    sock.sendto(PROBE, (t, port))
                except OSError as exc:
                    print("send to", t, "failed:", exc, flush=True)
            next_send = time.monotonic() + 2
        try:
            data, addr = sock.recvfrom(2048)
        except TimeoutError:
            continue
        except OSError as exc:
            print("receive error:", exc, flush=True)
            continue
        print("answer from", addr, data.decode(errors="replace"), flush=True)
    sock.close()


if __name__ == "__main__":
    main()
