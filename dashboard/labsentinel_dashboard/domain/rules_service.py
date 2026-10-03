"""Authoritative ruleset management on the dashboard.

Every change produces a new, strictly increasing revision that is persisted
(full history in ``rulesets``) and audited. Agents ignore stale revisions, so
pushing the latest revision is always safe and idempotent.
"""

from __future__ import annotations

import threading
from typing import Any

from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.protocol import HelloPayload
from labsentinel_shared.rules import RuleSet


class RulesService:
    def __init__(self, db: Database, *, default: RuleSet) -> None:
        self._db = db
        self._lock = threading.Lock()
        stored = db.latest_ruleset()
        if stored is not None:
            self._current = RuleSet.model_validate_json(stored)
        else:
            self._current = default
            db.save_ruleset(
                revision=default.revision, body=default.model_dump_json(), created_by="system"
            )

    def current(self) -> RuleSet:
        with self._lock:
            return self._current

    def ruleset_for(self, hello: HelloPayload) -> RuleSet:
        """RulesetProvider for the WebSocket server (same rules for every PC)."""
        return self.current()

    def update(self, *, actor: str, **changes: Any) -> RuleSet:
        """Apply field changes, bump the revision, persist and audit."""
        with self._lock:
            data = self._current.model_dump()
            data.update(changes)
            data["revision"] = self._current.revision + 1
            new = RuleSet.model_validate(data)
            self._db.save_ruleset(
                revision=new.revision, body=new.model_dump_json(), created_by=actor
            )
            self._current = new
        self._db.audit(
            actor=actor,
            action="rules.update",
            target="all",
            details={"revision": new.revision, "fields": sorted(changes)},
            result="ok",
        )
        return new

    def replace(self, ruleset: RuleSet, *, actor: str) -> RuleSet:
        """Replace the whole ruleset (e.g. import), keeping revisions monotonic."""
        fields = ruleset.model_dump()
        fields.pop("revision", None)
        return self.update(actor=actor, **fields)
