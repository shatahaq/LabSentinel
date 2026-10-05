"""Rule data models: the whitelist, the site blocklist, and the active profile.

These are the authoritative, serializable definitions pushed from the dashboard
to agents inside a ``rules_update`` message and cached locally by the agent so
it keeps enforcing the last known ruleset while disconnected.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from labsentinel_shared.enums import EnforcementMode

# Process names that the agent must NEVER touch, regardless of configuration.
# This is a hardcoded safety floor — the admin cannot remove entries from it.
# (Enforced in the agent's safety layer; duplicated here so the shared layer can
# validate that an admin-supplied whitelist/blocklist never tries to override
# system safety.)
PROTECTED_PROCESSES: frozenset[str] = frozenset(
    {
        # core OS
        "system",
        "system idle process",
        "registry",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "winlogon.exe",
        "services.exe",
        "lsass.exe",
        "lsaiso.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "sihost.exe",
        "taskhostw.exe",
        "ctfmon.exe",
        "spoolsv.exe",
        "explorer.exe",
        "runtimebroker.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "applicationframehost.exe",  # Store-app host; handled specially, never killed
        "textinputhost.exe",
        "useroobebroker.exe",
        "dllhost.exe",
        "conhost.exe",
        "audiodg.exe",
        "wudfhost.exe",
        # security / management agents commonly present in labs
        "msmpeng.exe",  # Defender
        "nissrv.exe",
        "securityhealthservice.exe",
        "securityhealthsystray.exe",
        "mpdefendercoreservice.exe",
        # Lab Sentinel's own binaries (filled defensively; agent also self-excludes by PID)
        "labsentinel-agent.exe",
        "labsentinel-service.exe",
        "labsentinel-tray.exe",  # the participant's indicator must never be closed
        "labsentinel-watchdog.exe",
    }
)

# SiteRule.category of the sites an admin types in by hand (not a preset).
CUSTOM_SITE_CATEGORY = "kustom"

# Browsers that ship as the immovable default whitelist.
DEFAULT_WHITELIST: frozenset[str] = frozenset({"chrome.exe", "msedge.exe", "explorer.exe"})


class AppRule(BaseModel):
    """A single allowed application.

    An app may be allowed by name alone, or pinned to a verified identity
    (expected path and/or SHA-256) so a renamed game cannot impersonate it.
    """

    model_config = ConfigDict(frozen=True)

    process_name: str = Field(min_length=1, max_length=260)
    display_name: str = Field(default="", max_length=120)
    # Optional hardening: if set, the running exe must match these to be allowed.
    expected_sha256: str | None = Field(default=None)
    expected_path_contains: str | None = Field(default=None, max_length=260)

    @field_validator("process_name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("expected_sha256")
    @classmethod
    def _check_hash(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("expected_sha256 must be 64 hex chars")
        return v


class SiteRule(BaseModel):
    """A blocked-site entry.

    ``pattern`` is a Chrome/Edge URLBlocklist pattern (e.g. ``example.com`` or
    ``*://*.example.com/*``). ``keywords`` feed the supplementary, lower-precision
    window-title detector.
    """

    model_config = ConfigDict(frozen=True)

    pattern: str = Field(min_length=1, max_length=512)
    category: str = Field(default="lainnya", max_length=60)
    keywords: tuple[str, ...] = Field(default=())

    @field_validator("keywords")
    @classmethod
    def _lower_keywords(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(k.strip().lower() for k in v if k.strip())


class RuleSet(BaseModel):
    """The complete, versioned ruleset handed to an agent."""

    model_config = ConfigDict(frozen=True)

    revision: int = Field(ge=0, description="Monotonic; agent ignores stale revisions.")
    mode: EnforcementMode = EnforcementMode.LAB
    dry_run: bool = Field(default=True, description="Audit-only; default-on for first install.")
    close_grace_seconds: int = Field(default=5, ge=0, le=60)
    app_whitelist: tuple[AppRule, ...] = Field(default=())
    site_blocklist: tuple[SiteRule, ...] = Field(default=())
    block_incognito: bool = True

    def effective_whitelist_names(self) -> frozenset[str]:
        """Allowed process names, always including the immovable defaults.

        Ujian (exam) allows the browsers only: the coursework apps on the list
        are for Praktikum and stay saved for it, but do not apply.
        """
        if self.mode is EnforcementMode.EXAM:
            return DEFAULT_WHITELIST
        names = {r.process_name for r in self.app_whitelist}
        return DEFAULT_WHITELIST | names

    @property
    def acts(self) -> bool:
        """Whether apps are closed and sites blocked, or only recorded.

        Recording only: in test mode (``dry_run``) and in Bebas (free), which
        is for watching the screens without restricting anything.
        """
        return not self.dry_run and self.mode is not EnforcementMode.FREE

    @staticmethod
    def exam_defaults(revision: int) -> RuleSet:
        """Strictest profile: browsers only."""
        return RuleSet(revision=revision, mode=EnforcementMode.EXAM, dry_run=False)
