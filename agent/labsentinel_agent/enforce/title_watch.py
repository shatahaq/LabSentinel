"""Supplementary site-violation detection from the active window title.

The browser policy does the actual blocking. This watcher only *records* a
suspected visit by reading the foreground window's title and matching it against
the blocklist's keywords/domains.

It is deliberately conservative and clearly labelled, because the signal is weak:
a window title is not a URL. A page merely *mentioning* a blocked site in its
title produces a match, and a blocked page that was never opened produces none.
Violations from here carry ``detected_from_title=True`` so the dashboard can
present them as "terdeteksi dari judul jendela" rather than as hard evidence.

To avoid flooding the dashboard, the same title is reported at most once per
dedup window, and only while a browser is in the foreground.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from labsentinel_shared.rules import RuleSet, SiteRule

log = logging.getLogger("labsentinel.agent.titlewatch")

# Only these foreground processes are considered; the policy layer only covers
# Chromium browsers anyway, and other apps produce meaningless matches.
BROWSER_PROCESSES: frozenset[str] = frozenset({"chrome.exe", "msedge.exe"})

# Chrome/Edge append this to the window title; strip it before matching so the
# browser's own name never counts as page content.
_TITLE_SUFFIXES = (
    " - google chrome",
    " - microsoft edge",
    " and 1 more page - personal - microsoft edge",
)

# Matches "https://" and also the wildcard form "*://" used in URLBlocklist.
_SCHEME_RE = re.compile(r"^[a-z*]+://", re.I)
# Tokens that must never trigger a match on their own. Two groups:
#   * structural parts of a hostname (TLDs, "www"), and
#   * generic words and big-platform parents that appear in countless page
#     titles. Without these, "gemini.google.com" would flag "Google Classroom"
#     and "chat.openai.com" would flag any page with "chat" in the title.
_NOISE = {
    # structural / TLDs
    "www", "com", "net", "org", "co", "id", "io", "ac", "ai", "tv", "me", "gg",
    "ly", "cc", "uk", "us", "info", "biz", "edu", "gov", "site", "online", "xyz",
    "html", "php",
    # platform parents that are not distinctive by themselves
    "google", "microsoft", "apple", "amazon", "yahoo",
    # generic subdomains / words
    "app", "web", "chat", "mail", "play", "my", "api", "cdn", "static", "docs",
    "drive", "login", "account", "home", "news", "video", "music", "live",
    "store", "shop", "support", "help", "blog", "forum", "wiki", "search",
    "image", "images", "portal", "student", "class",
}

# Shortest token we are willing to match on. One-character labels (x.com) are
# hopeless in a window title, so such sites rely on policy blocking alone.
_MIN_TOKEN_LEN = 2


@dataclass(frozen=True)
class TitleHit:
    """A suspected blocked-site visit inferred from a window title."""

    title: str
    matched: str  # the keyword/domain token that matched
    category: str


def domain_tokens(pattern: str) -> set[str]:
    """Reduce a URLBlocklist pattern to meaningful name tokens.

    ``*://*.facebook.com/*`` -> ``{"facebook"}``;  ``chat.openai.com`` ->
    ``{"chat", "openai"}``. Generic parts (www, com, id, ...) are dropped so a
    title cannot match on them.
    """
    p = _SCHEME_RE.sub("", pattern.strip().lower())
    p = p.split("/", 1)[0]  # drop path
    p = p.lstrip("*.").strip("*")
    p = p.split(":", 1)[0]  # drop port
    return {t for t in p.split(".") if t not in _NOISE and len(t) >= _MIN_TOKEN_LEN}


def normalize_title(title: str) -> str:
    t = title.strip().lower()
    for suffix in _TITLE_SUFFIXES:
        if t.endswith(suffix):
            t = t[: -len(suffix)]
            break
    return t.strip()


def match_title(title: str, rules: tuple[SiteRule, ...]) -> TitleHit | None:
    """Return the first rule token found in the title, or None."""
    clean = normalize_title(title)
    if not clean:
        return None
    for rule in rules:
        for keyword in rule.keywords:
            if keyword and keyword in clean:
                return TitleHit(title=title, matched=keyword, category=rule.category)
        for token in domain_tokens(rule.pattern):
            if token in clean:
                return TitleHit(title=title, matched=token, category=rule.category)
    return None


# (process_name, window_title) of the foreground window.
ForegroundProvider = Callable[[], tuple[str, str]]
HitSink = Callable[[TitleHit], Awaitable[None]]


class TitleWatcher:
    def __init__(
        self,
        *,
        foreground: ForegroundProvider,
        ruleset_provider: Callable[[], RuleSet | None],
        enabled: Callable[[], bool],
        on_hit: HitSink,
        interval_s: float = 2.0,
        dedup_window_s: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._foreground = foreground
        self._ruleset = ruleset_provider
        self._enabled = enabled
        self._on_hit = on_hit
        self._interval = interval_s
        self._dedup_window = dedup_window_s
        self._clock = clock
        self._sleep = sleep
        self._recent: dict[str, float] = {}
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        while not self._stop:
            await self._sleep(self._interval)
            if self._stop:
                break
            try:
                await self.check_once()
            except Exception:
                log.warning("title watch tick failed", exc_info=True)

    async def check_once(self) -> TitleHit | None:
        """Evaluate the foreground window once. Returns the hit it reported."""
        if not self._enabled():
            return None
        ruleset = self._ruleset()
        if ruleset is None or not ruleset.site_blocklist:
            return None

        process, title = self._foreground()
        if (process or "").strip().lower() not in BROWSER_PROCESSES:
            return None

        hit = match_title(title, ruleset.site_blocklist)
        if hit is None:
            return None
        if self._is_duplicate(normalize_title(title)):
            return None
        await self._on_hit(hit)
        return hit

    def _is_duplicate(self, key: str) -> bool:
        now = self._clock()
        for old, seen in list(self._recent.items()):
            if now - seen > self._dedup_window:
                del self._recent[old]
        if key in self._recent:
            return True
        self._recent[key] = now
        return False
