"""Aturan page: modes, allowed apps and blocked sites, as PCs receive them."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from labsentinel_dashboard.app import EXPOSED
from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.site_presets import ALL_CATEGORIES, BY_KEY, custom_site
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.ui.bridge import UiBridge
from labsentinel_shared.enums import EnforcementMode
from labsentinel_shared.rules import RuleSet


class PushRecorder:
    """Stands in for DashboardService: remembers what was sent to the PCs."""

    def __init__(self) -> None:
        self.pushed: list[RuleSet] = []

    async def push_rules(self, ruleset: RuleSet) -> int:
        self.pushed.append(ruleset)
        return 3


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[UiBridge, RulesService, PushRecorder]]:
    db = Database(tmp_path / "rules.sqlite")
    rules = RulesService(db, default=RuleSet(revision=0, mode=EnforcementMode.LAB))
    pcs = PushRecorder()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    unused: Any = None
    bridge = UiBridge(service=pcs, sessions=unused, rules=rules, db=db,  # type: ignore[arg-type]
                      bus=EventBus(), loop=loop, pairing=None)
    yield bridge, rules, pcs
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)
    loop.close()
    db.close()


def test_rule_methods_are_exposed_to_the_ui() -> None:
    for name in ("set_mode", "set_whitelist", "set_site_categories", "set_custom_sites",
                 "freeze_input"):
        assert name in EXPOSED


@pytest.mark.parametrize(("typed", "site"), [
    ("tiktok.com", "tiktok.com"),
    ("  https://www.TikTok.com/@someone/video/1?lang=id ", "tiktok.com"),
    ("chat.openai.com", "chat.openai.com"),
    ("*.example.org", "example.org"),
    ("192.168.1.5", "192.168.1.5"),
    ("tiktok", None),
    ("situs judi.com", None),
    ("300.1.1.1", None),
])
def test_a_typed_or_pasted_site_becomes_the_whole_site(typed: str, site: str | None) -> None:
    rule = custom_site(typed)
    assert (rule.pattern if rule else None) == site


def test_added_sites_and_chosen_categories_do_not_replace_each_other(setup) -> None:
    bridge, _, pcs = setup
    assert bridge.set_site_categories(["game_online"])["ok"]
    r = bridge.set_custom_sites(["tiktok.com", "https://www.instagram.com/"])
    assert r["ok"] and r["rules"]["custom_sites"] == ["tiktok.com", "instagram.com"]
    assert r["rules"]["site_categories"] == ["game online"]  # still chosen

    r = bridge.set_site_categories(["ai_chatbot"])  # change categories...
    assert r["rules"]["custom_sites"] == ["tiktok.com", "instagram.com"]  # ...added stay
    sent = {x.pattern for x in pcs.pushed[-1].site_blocklist}
    assert {"tiktok.com", "instagram.com"} <= sent
    assert {x.pattern for x in BY_KEY["ai_chatbot"].rules} <= sent
    assert not {x.pattern for x in BY_KEY["game_online"].rules} & sent

    r = bridge.set_custom_sites(["instagram.com"])  # remove one
    assert r["rules"]["custom_sites"] == ["instagram.com"]
    assert "tiktok.com" not in {x.pattern for x in pcs.pushed[-1].site_blocklist}


def test_a_site_that_is_not_an_address_is_refused(setup) -> None:
    bridge, rules, pcs = setup
    before = rules.current().revision
    r = bridge.set_custom_sites(["tiktok.com", "bukan alamat"])
    assert not r["ok"] and "bukan alamat" in r["error"]
    assert rules.current().revision == before and pcs.pushed == []


def test_adding_a_site_twice_keeps_one(setup) -> None:
    bridge, _, _ = setup
    r = bridge.set_custom_sites(["tiktok.com", "www.tiktok.com", "https://tiktok.com/x"])
    assert r["rules"]["custom_sites"] == ["tiktok.com"]


def test_apps_can_be_added_and_removed(setup) -> None:
    bridge, _, pcs = setup
    assert bridge.set_whitelist(["code.exe", "xampp-control.exe"])["rules"]["app_whitelist"] == [
        "code.exe", "xampp-control.exe"]
    assert bridge.set_whitelist(["code.exe"])["rules"]["app_whitelist"] == ["code.exe"]
    assert [a.process_name for a in pcs.pushed[-1].app_whitelist] == ["code.exe"]


def test_exam_blocks_every_category_plus_added_sites_and_keeps_lab_choices(setup) -> None:
    bridge, _, pcs = setup
    bridge.set_site_categories(["game_online"])
    bridge.set_custom_sites(["tiktok.com"])
    bridge.set_whitelist(["code.exe"])

    r = bridge.set_mode("exam")
    assert r["ok"]
    sent = pcs.pushed[-1]
    every = {x.pattern for c in ALL_CATEGORIES for x in c.rules}
    assert {x.pattern for x in sent.site_blocklist} == every | {"tiktok.com"}
    assert sent.effective_whitelist_names() == {"chrome.exe", "msedge.exe", "explorer.exe"}
    assert r["rules"]["site_count"] == len(every | {"tiktok.com"})

    # Back to Praktikum: the choices made for it are still there.
    r = bridge.set_mode("lab")
    assert r["rules"]["site_categories"] == ["game online"]
    assert r["rules"]["custom_sites"] == ["tiktok.com"]
    assert "code.exe" in pcs.pushed[-1].effective_whitelist_names()


def test_free_mode_only_watches(setup) -> None:
    bridge, _, pcs = setup
    bridge.set_dry_run(False)
    r = bridge.set_mode("free")
    assert r["ok"] and r["rules"]["acts"] is False
    assert r["rules"]["site_count"] == 0  # nothing is blocked in Bebas
    assert pcs.pushed[-1].acts is False


def test_new_pcs_get_the_rules_of_the_current_mode(setup) -> None:
    bridge, rules, _ = setup
    bridge.set_custom_sites(["tiktok.com"])
    bridge.set_mode("exam")
    hello: Any = None
    assert len(rules.ruleset_for(hello).site_blocklist) > len(rules.current().site_blocklist)
