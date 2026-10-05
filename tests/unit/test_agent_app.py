"""Agent entry point (labsentinel_agent/app.py).

Identity and token files, the "not paired yet" path, how --pair interacts with
a token already on disk, and the one promise shutdown must keep no matter what
fails: the browser policy is restored.
"""

from __future__ import annotations

import asyncio
import logging
import stat
import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

import labsentinel_agent.app as app_mod
from labsentinel_agent.app import (
    EXIT_NOT_PAIRED,
    EXIT_OK,
    EXIT_PAIRING_REJECTED,
    LabSentinelAgent,
    read_token,
    stable_agent_id,
    store_token,
)
from labsentinel_agent.policy.registry import FakeRegistry
from labsentinel_shared.config import AgentConfig
from labsentinel_shared.protocol import HelloPayload

BLOCKED = ["*.facebook.com", "chat.openai.com"]


def _config(tmp_path: Path) -> AgentConfig:
    data = tmp_path / "agent"
    return AgentConfig(
        data_dir=data,
        token_file=data / "token",
        direct_url="ws://127.0.0.1:9",  # never dialled in these tests
    )


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    """One in-memory registry shared by every agent built in a test, so a
    'restart' sees what the previous run left behind."""
    reg = FakeRegistry()
    monkeypatch.setattr(app_mod, "_registry", lambda: reg)
    return reg


@pytest.fixture
def agent(tmp_path: Path, registry: FakeRegistry) -> LabSentinelAgent:
    return LabSentinelAgent(_config(tmp_path))


# ----------------------------------------------------------- agent id
def test_agent_id_is_created_once_and_kept(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = stable_agent_id(config)
    assert config.agent_id_path.read_text(encoding="utf-8") == first
    assert stable_agent_id(config) == first
    assert stable_agent_id(config) == first


def test_agent_id_starts_with_hostname_and_is_a_valid_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "LAB-A-07")
    agent_id = stable_agent_id(_config(tmp_path))
    assert agent_id.startswith("lab-a-07-")
    HelloPayload(agent_id=agent_id, hostname="LAB-A-07")  # fits the protocol


def test_agent_id_is_trimmed_to_protocol_limit_for_long_hostnames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_mod.socket, "gethostname", lambda: "X" * 100)
    agent_id = stable_agent_id(_config(tmp_path))
    assert len(agent_id) == 64
    HelloPayload(agent_id=agent_id, hostname="X" * 100)


def test_agent_ids_differ_between_machines_with_same_hostname(tmp_path: Path) -> None:
    """Reimaged PCs often share a hostname; the random suffix keeps them apart."""
    a = stable_agent_id(_config(tmp_path / "a"))
    b = stable_agent_id(_config(tmp_path / "b"))
    assert a != b


def test_blank_agent_id_file_is_replaced(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agent_id_path.parent.mkdir(parents=True)
    config.agent_id_path.write_text("  \n", encoding="utf-8")
    agent_id = stable_agent_id(config)
    assert agent_id.strip()
    assert config.agent_id_path.read_text(encoding="utf-8") == agent_id


def test_agent_id_survives_surrounding_whitespace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.agent_id_path.parent.mkdir(parents=True)
    config.agent_id_path.write_text("lab-pc-01-abcd1234\r\n", encoding="utf-8")
    assert stable_agent_id(config) == "lab-pc-01-abcd1234"


# --------------------------------------------------------------- token
def test_read_token_when_missing_or_blank(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert read_token(config) is None
    config.token_file.parent.mkdir(parents=True)
    config.token_file.write_text("   \n", encoding="utf-8")
    assert read_token(config) is None


def test_store_then_read_token(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store_token(config, "per-agent-token-value")  # creates the folder
    assert config.token_file.exists()
    assert read_token(config) == "per-agent-token-value"


def test_read_token_tolerates_a_trailing_newline(tmp_path: Path) -> None:
    """An admin restoring the file with Notepad adds CRLF; that must not unpair."""
    config = _config(tmp_path)
    config.token_file.parent.mkdir(parents=True)
    config.token_file.write_text("per-agent-token-value\r\n", encoding="utf-8")
    assert read_token(config) == "per-agent-token-value"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_stored_token_is_private_to_its_owner(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store_token(config, "per-agent-token-value")
    assert stat.S_IMODE(config.token_file.stat().st_mode) == 0o600
    # No temp files left behind next to it.
    assert [p.name for p in config.token_file.parent.iterdir()] == ["token"]


def test_store_token_replaces_previous(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store_token(config, "old")
    store_token(config, "new")
    assert read_token(config) == "new"


# ------------------------------------------------------ not paired yet
async def test_unpaired_agent_says_so_and_exits_with_code_2(
    agent: LabSentinelAgent, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await agent.run()
    await agent.shutdown()
    assert code == EXIT_NOT_PAIRED
    assert agent.client is None  # never tried to connect without credentials
    out = capsys.readouterr().out
    assert "PC ini belum dipasangkan" in out
    assert "--pair" in out


def test_main_exits_with_code_2_when_not_paired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(app_mod, "setup_logging", lambda **_: None)  # keep pytest's handlers
    data = (tmp_path / "agent").as_posix()
    toml = tmp_path / "agent.toml"
    toml.write_text(f'data_dir = "{data}"\ntoken_file = "{data}/token"\n', encoding="utf-8")
    assert app_mod.main(["--config", str(toml)]) == EXIT_NOT_PAIRED
    assert "belum dipasangkan" in capsys.readouterr().out


# ----------------------------------------------- --pair vs stored token
class _RecordingClient:
    """Stands in for DashboardClient: records how it was built, ends at once."""

    built: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingClient.built.append(kwargs)
        self.pairing_rejected: str | None = None
        self.connected = asyncio.Event()

    async def run(self, url_provider: Any) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingClient]:
    _RecordingClient.built = []
    monkeypatch.setattr(app_mod, "DashboardClient", _RecordingClient)
    # Keep detection/streaming out of it; the link ending is what ends run().
    return _RecordingClient


async def test_stored_token_is_used_when_no_code_given(
    tmp_path: Path, registry: FakeRegistry, recording_client: type[_RecordingClient]
) -> None:
    config = _config(tmp_path)
    store_token(config, "stored-token")
    agent = LabSentinelAgent(config)
    assert await agent.run() == EXIT_OK
    await agent.shutdown()
    built = recording_client.built[0]
    assert built["token"] == "stored-token"
    assert built["pairing_code"] is None


async def test_pair_flag_ignores_a_stale_stored_token(
    tmp_path: Path, registry: FakeRegistry, recording_client: type[_RecordingClient]
) -> None:
    """After the dashboard database is reset, the old token is unknown. Running
    with --pair must actually present the code, not the dead token again."""
    config = _config(tmp_path)
    store_token(config, "token-from-a-reset-dashboard")
    agent = LabSentinelAgent(config, pairing_code="482913")
    await agent.run()
    await agent.shutdown()
    built = recording_client.built[0]
    assert built["token"] is None
    assert built["pairing_code"] == "482913"


async def test_issued_token_is_stored_and_announced(
    tmp_path: Path, registry: FakeRegistry, recording_client: type[_RecordingClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    agent = LabSentinelAgent(config, pairing_code="482913")
    await agent.run()
    recording_client.built[0]["on_token_issued"]("fresh-token")
    await agent.shutdown()
    assert read_token(config) == "fresh-token"
    assert "berhasil dipasangkan" in capsys.readouterr().out


async def test_rejected_pairing_explains_and_exits_with_code_3(
    tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Refused(_RecordingClient):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.pairing_rejected = "kode pemasangan salah"

    monkeypatch.setattr(app_mod, "DashboardClient", Refused)
    agent = LabSentinelAgent(_config(tmp_path), pairing_code="000000")
    assert await agent.run() == EXIT_PAIRING_REJECTED
    await agent.shutdown()
    out = capsys.readouterr().out
    assert "Pemasangan ditolak" in out
    assert "kode pemasangan salah" in out
    assert read_token(_config(tmp_path)) is None


# ------------------------------------- shutdown restores browser policy
def _policy_values(reg: FakeRegistry) -> dict[Any, Any]:
    """Browser policy only. (The agent's own status key under
    SOFTWARE\\LabSentinel is expected to remain, saying it manages nothing.)"""
    return {
        k: v for k, v in reg.dump().items()
        if v and k[1].lower().startswith("software\\policies")
    }


async def test_shutdown_restores_browser_policy(
    agent: LabSentinelAgent, registry: FakeRegistry
) -> None:
    before = _policy_values(registry)
    agent.policy.apply(BLOCKED)
    assert _policy_values(registry) != before  # sites really were blocked
    await agent.shutdown()
    assert _policy_values(registry) == before
    assert not agent.policy.has_backup


async def test_shutdown_restores_the_institutions_own_policy(
    agent: LabSentinelAgent, registry: FakeRegistry
) -> None:
    """A lab that already blocks sites via its own GPO keeps exactly that."""
    from labsentinel_agent.policy.browser_policy import BROWSERS
    from labsentinel_agent.policy.registry import Hive, ValueType

    path = BROWSERS["chrome"]["blocklist_path"]
    registry.write_value(Hive.HKLM, path, "1", "*.instagram.com", ValueType.SZ)
    before = _policy_values(registry)
    agent.policy.apply(BLOCKED)
    await agent.shutdown()
    assert _policy_values(registry) == before


@pytest.mark.parametrize("failing", ["titles", "streamer", "core"])
async def test_shutdown_restores_policy_even_when_a_component_fails_to_stop(
    agent: LabSentinelAgent, registry: FakeRegistry, failing: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = _policy_values(registry)
    agent.policy.apply(BLOCKED)

    def boom() -> None:
        raise RuntimeError(f"{failing} would not stop")

    getattr(agent, failing).stop = boom
    caplog.set_level(logging.ERROR)
    await agent.shutdown()  # must not raise
    assert _policy_values(registry) == before
    assert f"{failing} would not stop" in caplog.text


async def test_shutdown_restores_policy_even_when_the_link_and_tray_fail(
    agent: LabSentinelAgent, registry: FakeRegistry
) -> None:
    class Broken:
        async def stop(self) -> None:
            raise OSError("socket already gone")

    before = _policy_values(registry)
    agent.policy.apply(BLOCKED)
    agent.client = Broken()  # type: ignore[assignment]
    agent.tray.stop = Broken().stop  # type: ignore[method-assign]
    await agent.shutdown()
    assert _policy_values(registry) == before


async def test_policy_that_cannot_be_cleared_now_is_restored_on_next_start(
    tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _policy_values(registry)
    first = LabSentinelAgent(_config(tmp_path))
    first.policy.apply(BLOCKED)

    def denied() -> None:
        raise PermissionError("registry access denied")

    monkeypatch.setattr(first.policy, "clear", denied)
    await first.shutdown()  # logs, does not raise, keeps the backup
    assert first.policy.has_backup
    assert _policy_values(registry) != before

    second = LabSentinelAgent(_config(tmp_path))
    await second.run()  # recovery happens before anything else (even unpaired)
    await second.shutdown()
    assert _policy_values(registry) == before
    assert not second.policy.has_backup


async def test_policy_left_by_a_crash_is_restored_on_start(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    before = _policy_values(registry)
    crashed = LabSentinelAgent(_config(tmp_path))
    crashed.policy.apply(BLOCKED)  # ...and the process dies without shutdown()

    restarted = LabSentinelAgent(_config(tmp_path))
    await restarted.run()
    await restarted.shutdown()
    assert _policy_values(registry) == before
