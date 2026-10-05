"""Versioned wire protocol for agent <-> dashboard messaging.

Every message is a JSON ``Envelope`` carrying a protocol version, a type
discriminator, a unique id (for ACK correlation and replay detection), a
timestamp, and a strongly-typed payload. Payloads are validated with pydantic
so a malformed or hostile message is rejected at the boundary, never acted on.

Design notes
------------
* ``PROTOCOL_VERSION`` is bumped on any breaking change; a peer rejects an
  envelope whose major version it does not speak (see ``REJECT`` handshake).
* The auth token is carried on the envelope. It is validated by the transport
  layer *before* the payload is trusted; it is never logged.
* Timestamps + ids let the receiver drop replays within a short window.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from labsentinel_shared.enums import (
    AgentState,
    CommandType,
    EnforcementMode,
    MsgType,
    ViolationKind,
)
from labsentinel_shared.rules import RuleSet

PROTOCOL_VERSION = 1

# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- agent -> dashboard ----------------------------------------------------


class HelloPayload(_Payload):
    """First message from an agent: identify + request to join a session."""

    type: Literal[MsgType.HELLO] = MsgType.HELLO
    agent_id: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=128)
    username: str = Field(default="", max_length=128)
    agent_version: str = Field(default="0.0.0", max_length=32)
    os_version: str = Field(default="", max_length=64)
    cached_rule_revision: int = Field(default=-1, ge=-1)
    # Sent only on first contact, when the agent has no token yet. The short
    # code is exchanged for a strong per-agent token (see WelcomePayload).
    pairing_code: str | None = Field(default=None, max_length=32, repr=False)


class HeartbeatPayload(_Payload):
    type: Literal[MsgType.HEARTBEAT] = MsgType.HEARTBEAT
    agent_id: str = Field(min_length=1, max_length=64)
    state: AgentState
    rule_revision: int = Field(ge=-1)
    cpu_percent: float = Field(default=0.0, ge=0.0, le=100.0)
    mem_mb: float = Field(default=0.0, ge=0.0)
    active_window_title: str = Field(default="", max_length=256)
    # False when the participant's tray indicator is not being shown (helper
    # missing or killed). Transparency requires the dashboard to surface this.
    indicator_visible: bool = True
    # False when the agent runs as a service and cannot currently see the
    # participant's desktop (no desktop worker): screenshots, window titles and
    # closing apps are unavailable until it is back. See ADR-0006.
    desktop_access: bool = True
    # True while the supervisor has frozen this PC's keyboard and mouse.
    input_frozen: bool = False
    # Who is signed in at the PC now ("" = nobody). A service starts before
    # anyone signs in and stays connected across sign-ins, so the name in
    # HELLO goes stale; None = not reported (an older agent).
    username: str | None = Field(default=None, max_length=256)


class ViolationPayload(_Payload):
    type: Literal[MsgType.VIOLATION] = MsgType.VIOLATION
    agent_id: str = Field(min_length=1, max_length=64)
    kind: ViolationKind
    subject: str = Field(max_length=512, description="app/process name or site/title text")
    detail: str = Field(default="", max_length=1024)
    detected_from_title: bool = Field(default=False)
    dry_run: bool = Field(default=False, description="True = would-have-acted only.")
    occurred_at: float
    # Optional JPEG evidence, base64; kept out of the hot heartbeat path.
    screenshot_b64: str | None = Field(default=None)


class ScreenFramePayload(_Payload):
    type: Literal[MsgType.SCREEN_FRAME] = MsgType.SCREEN_FRAME
    agent_id: str = Field(min_length=1, max_length=64)
    hi_res: bool = False
    width: int = Field(gt=0, le=7680)
    height: int = Field(gt=0, le=4320)
    jpeg_b64: str = Field(min_length=1)
    captured_at: float


class AckPayload(_Payload):
    """Agent confirms it applied a command (by the command's envelope id)."""

    type: Literal[MsgType.ACK] = MsgType.ACK
    agent_id: str = Field(min_length=1, max_length=64)
    ack_id: str = Field(min_length=1, max_length=64)
    ok: bool = True
    resulting_state: AgentState
    message: str = Field(default="", max_length=256)


class GoodbyePayload(_Payload):
    type: Literal[MsgType.GOODBYE] = MsgType.GOODBYE
    agent_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="shutdown", max_length=128)


# --- dashboard -> agent ----------------------------------------------------


class WelcomePayload(_Payload):
    type: Literal[MsgType.WELCOME] = MsgType.WELCOME
    session_id: str = Field(min_length=1, max_length=64)
    server_time: float
    heartbeat_interval_s: float = Field(default=5.0, gt=0, le=60)
    ruleset: RuleSet
    # Present only right after a successful pairing: the long-lived credential
    # this agent must store and use from now on. Never logged.
    agent_token: str | None = Field(default=None, repr=False)


class RejectPayload(_Payload):
    type: Literal[MsgType.REJECT] = MsgType.REJECT
    reason: str = Field(max_length=256)
    server_protocol_version: int = PROTOCOL_VERSION


class RulesUpdatePayload(_Payload):
    type: Literal[MsgType.RULES_UPDATE] = MsgType.RULES_UPDATE
    ruleset: RuleSet


class CommandPayload(_Payload):
    type: Literal[MsgType.COMMAND] = MsgType.COMMAND
    command: CommandType
    # Free-form, command-specific args (validated per-command by the agent).
    text: str = Field(default="", max_length=1024)  # for broadcast_message
    mode: EnforcementMode | None = None
    issued_by: str = Field(default="", max_length=128)  # admin identity, for audit


AnyPayload = Annotated[
    HelloPayload
    | HeartbeatPayload
    | ViolationPayload
    | ScreenFramePayload
    | AckPayload
    | GoodbyePayload
    | WelcomePayload
    | RejectPayload
    | RulesUpdatePayload
    | CommandPayload,
    Field(discriminator="type"),
]

# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Envelope(BaseModel):
    """The only object that ever touches the socket."""

    model_config = ConfigDict(extra="forbid")

    v: int = Field(default=PROTOCOL_VERSION, description="Protocol version.")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = Field(default_factory=time.time)
    # Auth token: validated by transport, never logged. Optional on the model so
    # tests can build envelopes without one; transport enforces presence.
    token: str | None = Field(default=None, repr=False)
    payload: AnyPayload

    def version_compatible(self) -> bool:
        return self.v == PROTOCOL_VERSION

    def to_json(self) -> str:
        return self.model_dump_json()

    def redacted(self) -> dict[str, object]:
        """Dict form safe for structured logging (token stripped)."""
        data = self.model_dump(mode="json")
        data.pop("token", None)
        # Screenshots/frames are large & sensitive; elide the blobs in logs.
        payload = data.get("payload")
        if isinstance(payload, dict):
            for big in ("screenshot_b64", "jpeg_b64"):
                if payload.get(big):
                    payload[big] = f"<{len(payload[big])} b64 chars elided>"
            # Credentials must never reach a log file, not even partially.
            for secret in ("pairing_code", "agent_token"):
                if payload.get(secret):
                    payload[secret] = "<redacted>"
        return data


_ENVELOPE_ADAPTER = TypeAdapter(Envelope)


class ProtocolError(ValueError):
    """Raised when bytes on the wire cannot be trusted as a valid envelope."""


def encode(payload: BaseModel, *, token: str | None = None) -> str:
    """Wrap a payload in an envelope and serialize to a JSON string."""
    env = Envelope(payload=payload, token=token)  # type: ignore[arg-type]
    return env.to_json()


def decode(raw: str | bytes, *, max_bytes: int = 32 * 1024 * 1024) -> Envelope:
    """Parse + validate an incoming message.

    Raises ``ProtocolError`` on anything malformed, oversized, or of the wrong
    shape. Callers treat that as "drop the message", never as a crash.
    """
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ProtocolError(f"message too large: {len(raw)} > {max_bytes} bytes")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("message is not valid UTF-8") from exc
    elif len(raw) > max_bytes:
        raise ProtocolError(f"message too large: {len(raw)} > {max_bytes} chars")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ProtocolError("top-level message must be a JSON object")

    try:
        return _ENVELOPE_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise ProtocolError(f"schema validation failed: {exc.error_count()} error(s)") from exc
