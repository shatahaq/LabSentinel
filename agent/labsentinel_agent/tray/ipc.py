"""IPC protocol between the agent service and the user-session tray helper.

Newline-delimited JSON over a loopback TCP connection. Messages are validated
with pydantic, so a malformed or hostile line is dropped rather than acted on.

Authority is deliberately one-directional:

* **service -> helper**: status, warning, notification — things to *display*.
* **helper -> service**: hello and heartbeat only — "I am showing the icon".

The service never accepts an instruction from the helper, so a participant who
replaces the helper cannot pause monitoring or change enforcement. The worst
they can do is stop displaying the indicator, which the service detects and
reports to the dashboard.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from labsentinel_shared.enums import AgentState

IPC_VERSION = 1
MAX_LINE_BYTES = 64 * 1024


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- service -> helper ------------------------------------------------------


class StatusMsg(_Msg):
    """Current agent status for the indicator."""

    kind: Literal["status"] = "status"
    state: AgentState
    connected: bool = False  # connected to the dashboard?
    dashboard_name: str = Field(default="", max_length=128)


class WarnMsg(_Msg):
    """An app is about to be closed; warn the participant first."""

    kind: Literal["warn"] = "warn"
    app: str = Field(max_length=260)
    seconds: int = Field(ge=0, le=60)
    reason: str = Field(default="", max_length=512)


class NotifyMsg(_Msg):
    """A message from the supervisor (broadcast) or the agent."""

    kind: Literal["notify"] = "notify"
    title: str = Field(max_length=128)
    message: str = Field(max_length=1024)


# --- helper -> service ------------------------------------------------------


class HelloMsg(_Msg):
    kind: Literal["hello"] = "hello"
    token: str = Field(max_length=256, repr=False)
    pid: int = Field(ge=0)
    username: str = Field(default="", max_length=128)


class AliveMsg(_Msg):
    """Helper confirms the indicator is still on screen."""

    kind: Literal["alive"] = "alive"


AnyMsg = Annotated[
    StatusMsg | WarnMsg | NotifyMsg | HelloMsg | AliveMsg,
    Field(discriminator="kind"),
]

_ADAPTER: TypeAdapter[AnyMsg] = TypeAdapter(AnyMsg)


class IpcError(ValueError):
    """Raised when a line cannot be trusted as a valid IPC message."""


def encode(msg: BaseModel) -> bytes:
    """Serialize a message as one newline-terminated JSON line."""
    return msg.model_dump_json().encode("utf-8") + b"\n"


def decode(line: bytes | str) -> AnyMsg:
    """Parse and validate one IPC line."""
    if isinstance(line, bytes):
        if len(line) > MAX_LINE_BYTES:
            raise IpcError("ipc line too large")
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IpcError("ipc line is not valid UTF-8") from exc
    line = line.strip()
    if not line:
        raise IpcError("empty ipc line")
    try:
        data = json.loads(line)
    except json.JSONDecodeError as exc:
        raise IpcError(f"invalid ipc JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise IpcError("ipc message must be a JSON object")
    try:
        parsed: AnyMsg = _ADAPTER.validate_python(data)
        return parsed
    except ValidationError as exc:
        raise IpcError(f"ipc schema validation failed: {exc.error_count()} error(s)") from exc
