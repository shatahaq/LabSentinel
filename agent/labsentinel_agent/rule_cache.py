"""Crash-safe local cache of the last ruleset received from the dashboard.

Lets the agent keep enforcing the most recent rules while disconnected, and
restore them immediately on restart. Writes are atomic (temp file + os.replace)
so a crash mid-write never corrupts the cache.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.rulecache")


class RuleCache:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._ruleset: RuleSet | None = None

    @property
    def current(self) -> RuleSet | None:
        return self._ruleset

    def load(self) -> RuleSet | None:
        """Load a persisted ruleset, or None if absent/corrupt."""
        try:
            data = self._path.read_text(encoding="utf-8")
            self._ruleset = RuleSet.model_validate_json(data)
            log.info("loaded cached ruleset revision %d", self._ruleset.revision)
        except FileNotFoundError:
            self._ruleset = None
        except Exception:
            log.warning("cached ruleset was corrupt; ignoring", exc_info=True)
            self._ruleset = None
        return self._ruleset

    def update(self, ruleset: RuleSet) -> bool:
        """Persist a newer ruleset. Ignores stale/older revisions.

        Returns True if the cache was updated.
        """
        if self._ruleset is not None and ruleset.revision < self._ruleset.revision:
            log.debug(
                "ignoring stale ruleset rev %d (have %d)",
                ruleset.revision,
                self._ruleset.revision,
            )
            return False
        self._atomic_write(ruleset.model_dump_json())
        self._ruleset = ruleset
        return True

    def _atomic_write(self, text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
