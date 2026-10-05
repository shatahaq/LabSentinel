"""Authoritative ruleset management on the dashboard.

Every change produces a new, strictly increasing revision that is persisted
(full history in ``rulesets``) and audited. Agents ignore stale revisions, so
pushing the latest revision is always safe and idempotent.
"""

from __future__ import annotations

import threading
from typing import Any

from labsentinel_dashboard.domain.site_presets import ALL_CATEGORIES, rules_for
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.enums import EnforcementMode
from labsentinel_shared.protocol import HelloPayload
from labsentinel_shared.rules import CUSTOM_SITE_CATEGORY, RuleSet, SiteRule


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
        return self.for_agents()

    def for_agents(self) -> RuleSet:
        """The ruleset PCs receive: the admin's settings applied to the mode."""
        return for_agents(self.current())

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


def custom_sites(ruleset: RuleSet) -> tuple[SiteRule, ...]:
    """The sites the admin added by hand (blocked in Praktikum and Ujian)."""
    return tuple(r for r in ruleset.site_blocklist if r.category == CUSTOM_SITE_CATEGORY)


def for_agents(ruleset: RuleSet) -> RuleSet:
    """What a mode means, in the ruleset agents enforce.

    The stored ruleset keeps the admin's choices for Praktikum (chosen site
    categories, added sites, allowed apps), so switching modes never loses
    them. Ujian blocks every category plus the added sites; the agent itself
    limits Ujian to the browsers and makes Bebas record-only
    (``RuleSet.effective_whitelist_names`` / ``RuleSet.acts``).
    """
    if ruleset.mode is not EnforcementMode.EXAM:
        return ruleset
    every_category = rules_for([c.key for c in ALL_CATEGORIES])
    known = {r.pattern for r in every_category}
    added = tuple(r for r in custom_sites(ruleset) if r.pattern not in known)
    return ruleset.model_copy(update={"site_blocklist": every_category + added})
