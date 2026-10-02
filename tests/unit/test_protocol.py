"""Protocol encode/decode + validation contract tests."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from labsentinel_shared.enums import AgentState, CommandType, MsgType, ViolationKind
from labsentinel_shared.protocol import (
    PROTOCOL_VERSION,
    AckPayload,
    CommandPayload,
    Envelope,
    HeartbeatPayload,
    HelloPayload,
    ProtocolError,
    ViolationPayload,
    WelcomePayload,
    decode,
    encode,
)
from labsentinel_shared.rules import RuleSet


def test_roundtrip_hello() -> None:
    payload = HelloPayload(agent_id="pc-01", hostname="LAB-PC-01", username="student")
    raw = encode(payload, token="secret-token")
    env = decode(raw)
    assert env.v == PROTOCOL_VERSION
    assert isinstance(env.payload, HelloPayload)
    assert env.payload.agent_id == "pc-01"
    assert env.token == "secret-token"


def test_discriminator_selects_correct_model() -> None:
    hb = HeartbeatPayload(agent_id="pc-01", state=AgentState.ENFORCING, rule_revision=3)
    env = decode(encode(hb))
    assert env.payload.type == MsgType.HEARTBEAT
    assert isinstance(env.payload, HeartbeatPayload)
    assert env.payload.state is AgentState.ENFORCING


def test_welcome_carries_ruleset() -> None:
    rs = RuleSet.exam_defaults(revision=7)
    env = decode(encode(WelcomePayload(session_id="s1", server_time=time.time(), ruleset=rs)))
    assert isinstance(env.payload, WelcomePayload)
    assert env.payload.ruleset.revision == 7
    assert env.payload.ruleset.dry_run is False


def test_each_envelope_has_unique_id() -> None:
    a = Envelope(payload=HelloPayload(agent_id="x", hostname="h"))
    b = Envelope(payload=HelloPayload(agent_id="x", hostname="h"))
    assert a.id != b.id


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "[]",  # not an object
        "123",
        '{"v": 1}',  # no payload
        '{"v": 1, "payload": {"type": "nope"}}',  # unknown type
        '{"v": 1, "payload": {"type": "hello"}}',  # missing required fields
    ],
)
def test_malformed_is_rejected_not_crashed(raw: str) -> None:
    with pytest.raises(ProtocolError):
        decode(raw)


def test_oversize_message_rejected() -> None:
    with pytest.raises(ProtocolError):
        decode("x" * 100, max_bytes=10)


def test_extra_fields_forbidden() -> None:
    # A hostile peer adding unexpected fields must be rejected.
    bad = (
        '{"v":1,"id":"a","ts":1.0,"payload":'
        '{"type":"hello","agent_id":"a","hostname":"h","surprise":true}}'
    )
    with pytest.raises(ProtocolError):
        decode(bad)


def test_field_bounds_enforced() -> None:
    # cpu_percent > 100 is invalid.
    with pytest.raises(ValidationError):
        HeartbeatPayload(agent_id="a", state=AgentState.PAUSED, rule_revision=0, cpu_percent=150)


def test_token_is_redacted_in_logs() -> None:
    env = Envelope(payload=HelloPayload(agent_id="a", hostname="h"), token="TOPSECRET")
    red = env.redacted()
    assert "token" not in red
    assert "TOPSECRET" not in repr(env)


def test_screenshot_blob_elided_in_logs() -> None:
    v = ViolationPayload(
        agent_id="a",
        kind=ViolationKind.APP_BLOCKED,
        subject="game.exe",
        occurred_at=time.time(),
        screenshot_b64="A" * 5000,
    )
    red = Envelope(payload=v).redacted()
    assert "elided" in red["payload"]["screenshot_b64"]  # type: ignore[index]


def test_command_ack_correlation_shape() -> None:
    cmd_env = Envelope(payload=CommandPayload(command=CommandType.PAUSE, issued_by="dosen@lab"))
    ack = AckPayload(agent_id="pc-01", ack_id=cmd_env.id, resulting_state=AgentState.PAUSED)
    assert ack.ack_id == cmd_env.id


def test_version_compatibility_check() -> None:
    env = Envelope(payload=HelloPayload(agent_id="a", hostname="h"))
    assert env.version_compatible()
    env.v = 999
    assert not env.version_compatible()
