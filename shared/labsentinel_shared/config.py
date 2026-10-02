"""Configuration models for the agent and dashboard.

Config lives in TOML files, never hardcoded. Secrets (the raw pairing token)
live in their own file referenced by path, so config can be committed/shared
while the token stays local. Models are validated on load; bad config fails
fast with a clear error.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field

from labsentinel_shared.discovery import DEFAULT_DISCOVERY_PORT
from labsentinel_shared.enums import EnforcementMode


class AgentConfig(BaseModel):
    """Agent runtime configuration (one lab PC)."""

    # Identity file persists a stable agent_id across restarts/reimaging.
    data_dir: Path = Field(default=Path("C:/ProgramData/LabSentinel/agent"))
    log_level: str = "INFO"

    # Discovery / connection
    discovery_port: int = Field(default=DEFAULT_DISCOVERY_PORT, gt=0, lt=65536)
    direct_url: str | None = None  # skip discovery if set, e.g. ws://10.0.0.5:8765
    discovery_timeout_s: float = Field(default=8.0, gt=0)

    # Auth: path to a file containing the raw pairing token (never inline).
    token_file: Path = Field(default=Path("C:/ProgramData/LabSentinel/agent/token"))

    # Behavior
    heartbeat_interval_s: float = Field(default=5.0, gt=0, le=60)
    close_grace_seconds: int = Field(default=5, ge=0, le=60)
    poll_fallback_interval_s: float = Field(default=3.0, gt=0, le=30)
    screen_grid_fps: float = Field(default=1.0, gt=0, le=5)
    screen_hi_res_fps: float = Field(default=5.0, gt=0, le=15)

    @property
    def rule_cache_path(self) -> Path:
        return self.data_dir / "ruleset.json"

    @property
    def agent_id_path(self) -> Path:
        return self.data_dir / "agent_id"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "agent.log"


class DashboardConfig(BaseModel):
    """Dashboard (supervisor PC) configuration."""

    data_dir: Path = Field(default=Path("C:/ProgramData/LabSentinel/dashboard"))
    log_level: str = "INFO"
    dashboard_name: str = "Lab Sentinel"

    # Agent-facing WebSocket server
    ws_host: str = "0.0.0.0"
    ws_port: int = Field(default=8765, gt=0, lt=65536)

    # UDP discovery beacon
    discovery_port: int = Field(default=DEFAULT_DISCOVERY_PORT, gt=0, lt=65536)
    beacon_interval_s: float = Field(default=3.0, gt=0, le=30)
    advertise_host: str | None = None  # LAN IP to advertise; autodetected if None

    # Local admin UI (Eel) — localhost only
    eel_host: str = "localhost"
    eel_port: int = Field(default=8080, gt=0, lt=65536)

    # Auth: SHA-256 hash of the shared pairing token (raw token never stored).
    token_hash_file: Path = Field(
        default=Path("C:/ProgramData/LabSentinel/dashboard/token_hash")
    )

    # Session defaults
    default_mode: EnforcementMode = EnforcementMode.LAB
    default_dry_run: bool = True  # audit-only on first install
    heartbeat_timeout_s: float = Field(default=15.0, gt=0)  # dropped if no HB within

    # Data lifecycle
    retention_days: int = Field(default=30, ge=1, le=3650)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "labsentinel.sqlite"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "logs" / "dashboard.log"


def _load_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_agent_config(path: Path | None) -> AgentConfig:
    if path is None or not path.exists():
        return AgentConfig()
    return AgentConfig.model_validate(_load_toml(path))


def load_dashboard_config(path: Path | None) -> DashboardConfig:
    if path is None or not path.exists():
        return DashboardConfig()
    return DashboardConfig.model_validate(_load_toml(path))
