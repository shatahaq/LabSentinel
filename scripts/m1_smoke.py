"""M1 live smoke demo: dashboard WS server + a real agent client, end to end.

Runs both sides in one process over loopback and prints a transcript proving:
pairing handshake, WELCOME ruleset delivery, heartbeats, a live rule update, and
a pause command with ACK. This is a manual demonstration (not a test); the
automated proof lives in tests/integration/test_agent_dashboard.py.

    python scripts/m1_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
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
from labsentinel_dashboard.transport.server import (  # noqa: E402
    AgentSession,
    WebSocketServer,
)
from labsentinel_shared.enums import AgentState, CommandType  # noqa: E402
from labsentinel_shared.protocol import (  # noqa: E402
    AckPayload,
    CommandPayload,
    HeartbeatPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import AppRule, RuleSet  # noqa: E402
from labsentinel_shared.security import hash_token, verify_token  # noqa: E402

TOKEN = "demo-pairing-token"
state = {"agent": AgentState.ENFORCING}


class PrintEvents:
    async def on_agent_connected(self, s: AgentSession) -> None:
        print(f"[dashboard] + agent connected: {s.agent_id} ({s.hostname}) from {s.remote_addr}")

    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None:
        print(f"[dashboard] - agent {agent_id} disconnected (clean={clean})")

    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
        print(
            f"[dashboard]   heartbeat {agent_id}: state={hb.state} rev={hb.rule_revision} "
            f"cpu={hb.cpu_percent:.1f}% mem={hb.mem_mb:.0f}MB"
        )

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        print(f"[dashboard]   VIOLATION {agent_id}: {v.kind} {v.subject}")

    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None:
        print(f"[dashboard]   frame {agent_id}: {f.width}x{f.height}")

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        print(f"[dashboard]   ACK {agent_id}: -> {ack.resulting_state} (ok={ack.ok})")


async def main() -> None:
    server = WebSocketServer(
        host="127.0.0.1",
        port=0,
        token_verifier=lambda t: verify_token(t, hash_token(TOKEN)),
        ruleset_provider=lambda hello: RuleSet(
            revision=1, dry_run=True, app_whitelist=(AppRule(process_name="code.exe"),)
        ),
        events=PrintEvents(),
        reply_token=TOKEN,
        heartbeat_interval_s=1.0,
    )
    await server.start()
    url = f"ws://127.0.0.1:{server.bound_port}"
    print(f"[dashboard] WS server listening at {url}\n")

    async def on_ruleset(rs: RuleSet) -> None:
        print(f"[agent]    applied ruleset rev={rs.revision} mode={rs.mode} dry_run={rs.dry_run}")

    async def on_command(cmd: CommandPayload) -> CommandResult:
        print(f"[agent]    got command {cmd.command} from {cmd.issued_by!r}")
        if cmd.command == CommandType.PAUSE:
            state["agent"] = AgentState.PAUSED
        elif cmd.command == CommandType.RESUME:
            state["agent"] = AgentState.ENFORCING
        return CommandResult(resulting_state=state["agent"])

    client = DashboardClient(
        identity=AgentIdentity(agent_id="pc-demo", hostname="LAB-DEMO", username="student"),
        token=TOKEN,
        state_provider=lambda: state["agent"],
        metrics_provider=lambda: (1.2, 38.0, "Google Chrome"),
        on_ruleset=on_ruleset,
        on_command=on_command,
        cached_revision_provider=lambda: 1,
        heartbeat_interval_s=1.0,
        backoff=BackoffPolicy(base_s=0.2, max_s=1.0, jitter_s=0.0),
    )
    run_task = asyncio.ensure_future(client.run(lambda: _ret(url)))

    await asyncio.wait_for(client.connected.wait(), timeout=5)
    await asyncio.sleep(2.5)  # let a couple of heartbeats flow

    print("\n[dashboard] pushing a stricter ruleset (rev 2, enforcing)...")
    session = server.get("pc-demo")
    assert session is not None
    await session.send_ruleset(RuleSet(revision=2, dry_run=False))
    await asyncio.sleep(0.5)

    print("[dashboard] sending PAUSE command...")
    await session.send_command(CommandPayload(command=CommandType.PAUSE, issued_by="dosen@lab"))
    await asyncio.sleep(1.5)  # show heartbeat now reports PAUSED

    print("\n[demo] shutting down...")
    await client.stop()
    await asyncio.wait_for(run_task, timeout=5)
    await server.stop()
    print("[demo] done.")


async def _ret(url: str) -> str:
    return url


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(main())
    print(f"[demo] elapsed {time.time() - t0:.1f}s")
