"""Shared enumerations used across the protocol, rules, and UI.

Kept in one place so the agent, dashboard, and tests agree on the exact string
values that travel over the wire and land in the database.
"""

from __future__ import annotations

from enum import StrEnum  # native since Python 3.11


class EnforcementMode(StrEnum):
    """Operating profile selected by the admin, pushed to agents."""

    EXAM = "exam"  # Ujian: browsers only, strictest site blocking
    LAB = "lab"  # Praktikum: browsers + coursework apps, medium blocking
    FREE = "free"  # Bebas: no restrictions


class AgentState(StrEnum):
    """The agent's own view of what it is doing right now.

    Reported in heartbeats and command ACKs so the dashboard shows *real*
    agent state, never just the last button pressed.
    """

    ENFORCING = "enforcing"  # actively applying rules + streaming when watched
    PAUSED = "paused"  # monitoring paused by admin; service/heartbeat alive
    DRY_RUN = "dry_run"  # audit-only: detects & logs but closes/blocks nothing


class ConnectionStatus(StrEnum):
    """Dashboard-side classification of each PC (derived from heartbeats)."""

    ONLINE = "online"
    OFFLINE = "offline"  # clean disconnect / shutdown
    DROPPED = "dropped"  # heartbeat timeout without a goodbye (possible tamper)
    PAUSED = "paused"


class ViolationKind(StrEnum):
    """What triggered a violation record."""

    APP_BLOCKED = "app_blocked"  # non-whitelisted app detected
    APP_CLOSED = "app_closed"  # non-whitelisted app actually closed
    IMPERSONATION = "impersonation"  # whitelisted name but failed path/hash verification
    SITE_TITLE = "site_title"  # suspected blocked site, from window title (low precision)
    AGENT_TAMPER = "agent_tamper"  # agent force-killed / watchdog restart


class CommandType(StrEnum):
    """Admin -> agent remote commands."""

    PAUSE = "pause"
    RESUME = "resume"
    SET_DRY_RUN = "set_dry_run"
    LOCK_SCREEN = "lock_screen"
    BROADCAST_MESSAGE = "broadcast_message"
    # Screen streaming level. Carried in the command's `text` as a StreamMode,
    # so the agent can stop capturing entirely when nobody is watching.
    SET_STREAM = "set_stream"


class StreamMode(StrEnum):
    """How much screen the agent should send right now."""

    OFF = "off"  # nobody is watching (or paused): capture nothing at all
    GRID = "grid"  # thumbnail in the monitoring grid: low fps, small
    HIRES = "hires"  # admin opened this PC full-screen: higher fps, larger


class MsgType(StrEnum):
    """Top-level message types on the wire (the envelope discriminator)."""

    # agent -> dashboard
    HELLO = "hello"
    HEARTBEAT = "heartbeat"
    VIOLATION = "violation"
    SCREEN_FRAME = "screen_frame"
    ACK = "ack"
    GOODBYE = "goodbye"

    # dashboard -> agent
    WELCOME = "welcome"
    REJECT = "reject"
    RULES_UPDATE = "rules_update"
    COMMAND = "command"
