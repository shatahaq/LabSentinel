"""Window-title site detection tests (the low-precision supplementary signal)."""

from __future__ import annotations

import pytest

from labsentinel_agent.enforce.title_watch import (
    TitleHit,
    TitleWatcher,
    domain_tokens,
    match_title,
    normalize_title,
)
from labsentinel_shared.rules import RuleSet, SiteRule

SOCIAL = SiteRule(pattern="*://*.facebook.com/*", category="media sosial", keywords=("facebook",))
AI = SiteRule(pattern="chat.openai.com", category="AI chatbot")
GAME = SiteRule(pattern="roblox.com", category="game online", keywords=("roblox",))
RULES = (SOCIAL, AI, GAME)


# ---------------------------------------------------------------- tokenizing
def test_domain_tokens_strips_scheme_wildcards_and_noise() -> None:
    assert domain_tokens("*://*.facebook.com/*") == {"facebook"}
    assert domain_tokens("https://www.detik.com/news") == {"detik"}
    # "chat" is a generic subdomain, so only the distinctive label survives.
    assert domain_tokens("chat.openai.com") == {"openai"}


def test_generic_parts_never_become_tokens() -> None:
    # Otherwise every page with "com" or "id" in the title would match.
    assert domain_tokens("www.com") == set()
    assert "com" not in domain_tokens("example.com")


def test_normalize_strips_browser_suffix() -> None:
    assert normalize_title("Facebook - Google Chrome") == "facebook"
    assert normalize_title("Tugas - Microsoft Edge") == "tugas"


# ----------------------------------------------------------------- matching
def test_matches_keyword() -> None:
    hit = match_title("Facebook - Google Chrome", RULES)
    assert hit is not None and hit.matched == "facebook" and hit.category == "media sosial"


def test_matches_domain_token_without_keywords() -> None:
    hit = match_title("ChatGPT - openai - Google Chrome", RULES)
    assert hit is not None and hit.category == "AI chatbot"


def test_no_match_on_ordinary_page() -> None:
    assert match_title("Modul Praktikum Basis Data - Google Chrome", RULES) is None


def test_browser_name_alone_is_not_a_match() -> None:
    assert match_title("Google Chrome", RULES) is None


def test_empty_title_is_not_a_match() -> None:
    assert match_title("   ", RULES) is None


# ------------------------------------------------------------------ watcher
def _watcher(**kw):
    defaults: dict[str, object] = {
        "foreground": lambda: ("chrome.exe", "Facebook - Google Chrome"),
        "ruleset_provider": lambda: RuleSet(revision=1, site_blocklist=RULES),
        "enabled": lambda: True,
        "interval_s": 0,
    }
    defaults.update(kw)
    hits: list[TitleHit] = []

    async def on_hit(h: TitleHit) -> None:
        hits.append(h)

    return TitleWatcher(on_hit=on_hit, **defaults), hits  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reports_hit_for_browser_foreground() -> None:
    w, hits = _watcher()
    assert await w.check_once() is not None
    assert len(hits) == 1 and hits[0].matched == "facebook"


@pytest.mark.asyncio
async def test_ignores_non_browser_foreground() -> None:
    """A text editor showing the word 'facebook' is not a browsing signal."""
    w, hits = _watcher(foreground=lambda: ("notepad.exe", "facebook notes.txt"))
    assert await w.check_once() is None
    assert hits == []


@pytest.mark.asyncio
async def test_disabled_when_paused() -> None:
    w, hits = _watcher(enabled=lambda: False)
    assert await w.check_once() is None
    assert hits == []


@pytest.mark.asyncio
async def test_no_ruleset_or_empty_blocklist_is_quiet() -> None:
    w, _ = _watcher(ruleset_provider=lambda: None)
    assert await w.check_once() is None
    w2, _ = _watcher(ruleset_provider=lambda: RuleSet(revision=1))
    assert await w2.check_once() is None


@pytest.mark.asyncio
async def test_same_title_deduplicated() -> None:
    w, hits = _watcher()
    await w.check_once()
    await w.check_once()
    await w.check_once()
    assert len(hits) == 1  # no dashboard spam


@pytest.mark.asyncio
async def test_dedup_expires_after_window() -> None:
    now = {"t": 1000.0}
    w, hits = _watcher(clock=lambda: now["t"], dedup_window_s=60.0)
    await w.check_once()
    now["t"] += 120
    await w.check_once()
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_different_titles_both_reported() -> None:
    titles = iter(["Facebook - Google Chrome", "Roblox - Google Chrome"])
    w, hits = _watcher(foreground=lambda: ("chrome.exe", next(titles)))
    await w.check_once()
    await w.check_once()
    assert [h.category for h in hits] == ["media sosial", "game online"]


@pytest.mark.asyncio
async def test_foreground_error_propagates_from_check_once() -> None:
    def boom() -> tuple[str, str]:
        raise OSError("no foreground window")

    w, _ = _watcher(foreground=boom)
    with pytest.raises(OSError):
        await w.check_once()


@pytest.mark.asyncio
async def test_run_loop_survives_foreground_errors() -> None:
    """A transient foreground-window error must not kill the watcher."""
    import asyncio

    def boom() -> tuple[str, str]:
        raise OSError("no foreground window")

    box: dict[str, TitleWatcher] = {}
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 3:
            box["w"].stop()

    watcher, _ = _watcher(foreground=boom, sleep=sleep)
    box["w"] = watcher
    await asyncio.wait_for(watcher.run(), timeout=2)
    assert ticks["n"] == 3  # looped, survived the errors, then stopped
