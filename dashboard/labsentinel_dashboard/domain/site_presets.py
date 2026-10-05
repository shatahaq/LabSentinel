"""Ready-made blocked-site categories the admin can switch on with one click.

Patterns use the Chrome/Edge ``URLBlocklist`` syntax. Keywords feed the
supplementary window-title detector, so they are written as they would appear in
a page title rather than as domains.

These lists are a starting point, not an exhaustive filter: new sites appear
constantly, which is exactly why the application whitelist — not this list — is
the primary safeguard. Admins can edit, extend, import and export them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from labsentinel_shared.rules import CUSTOM_SITE_CATEGORY, SiteRule


@dataclass(frozen=True)
class SiteCategory:
    key: str
    label: str  # shown in the UI (Indonesian)
    description: str
    rules: tuple[SiteRule, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "count": len(self.rules),
            "patterns": [r.pattern for r in self.rules],
        }


def _rules(category: str, entries: list[tuple[str, tuple[str, ...]]]) -> tuple[SiteRule, ...]:
    return tuple(
        SiteRule(pattern=pattern, category=category, keywords=keywords)
        for pattern, keywords in entries
    )


GAME_ONLINE = SiteCategory(
    key="game_online",
    label="Game online",
    description="Situs dan platform game yang bisa dimainkan langsung di browser.",
    rules=_rules(
        "game online",
        [
            ("*://*.roblox.com/*", ("roblox",)),
            ("*://*.miniclip.com/*", ("miniclip",)),
            ("*://*.crazygames.com/*", ("crazygames",)),
            ("*://*.poki.com/*", ("poki",)),
            ("*://*.y8.com/*", ("y8 games",)),
            ("*://*.friv.com/*", ("friv",)),
            ("*://*.agar.io/*", ("agar.io",)),
            ("*://*.slither.io/*", ("slither",)),
            ("*://*.steampowered.com/*", ("steam",)),
            ("*://*.epicgames.com/*", ("epic games",)),
            ("*://*.itch.io/*", ("itch.io",)),
            ("*://*.coolmathgames.com/*", ("coolmath",)),
        ],
    ),
)

MEDIA_SOSIAL = SiteCategory(
    key="media_sosial",
    label="Media sosial",
    description="Jejaring sosial, berbagi video pendek, dan aplikasi pesan berbasis web.",
    rules=_rules(
        "media sosial",
        [
            ("*://*.facebook.com/*", ("facebook",)),
            ("*://*.instagram.com/*", ("instagram",)),
            ("*://*.twitter.com/*", ("twitter",)),
            ("*://*.x.com/*", ()),
            ("*://*.tiktok.com/*", ("tiktok",)),
            ("*://*.snapchat.com/*", ("snapchat",)),
            ("*://*.reddit.com/*", ("reddit",)),
            ("*://*.pinterest.com/*", ("pinterest",)),
            ("*://web.whatsapp.com/*", ("whatsapp",)),
            ("*://*.telegram.org/*", ("telegram",)),
            ("*://web.telegram.org/*", ()),
            ("*://discord.com/app*", ("discord",)),
            ("*://*.threads.net/*", ("threads",)),
        ],
    ),
)

AI_CHATBOT = SiteCategory(
    key="ai_chatbot",
    label="AI chatbot",
    description="Asisten AI yang bisa mengerjakan soal atau menulis kode untuk peserta.",
    rules=_rules(
        "AI chatbot",
        [
            ("*://chat.openai.com/*", ("chatgpt",)),
            ("*://chatgpt.com/*", ()),
            ("*://claude.ai/*", ("claude",)),
            ("*://gemini.google.com/*", ("gemini",)),
            ("*://bard.google.com/*", ("bard",)),
            ("*://copilot.microsoft.com/*", ("copilot",)),
            ("*://*.perplexity.ai/*", ("perplexity",)),
            ("*://*.poe.com/*", ("poe",)),
            ("*://*.deepseek.com/*", ("deepseek",)),
            ("*://*.phind.com/*", ("phind",)),
            ("*://*.blackbox.ai/*", ("blackbox",)),
        ],
    ),
)

SITUS_JAWABAN = SiteCategory(
    key="situs_jawaban",
    label="Situs jawaban tugas",
    description="Situs berbagi kunci jawaban dan pekerjaan rumah.",
    rules=_rules(
        "situs jawaban",
        [
            ("*://*.chegg.com/*", ("chegg",)),
            ("*://*.coursehero.com/*", ("course hero",)),
            ("*://*.brainly.co.id/*", ("brainly",)),
            ("*://*.brainly.com/*", ()),
            ("*://*.quizlet.com/*", ("quizlet",)),
            ("*://*.studocu.com/*", ("studocu",)),
            ("*://*.ruangguru.com/*", ("ruangguru",)),
        ],
    ),
)

HIBURAN = SiteCategory(
    key="hiburan",
    label="Hiburan & streaming",
    description="Layanan video dan musik yang mengganggu fokus serta memakan bandwidth.",
    rules=_rules(
        "hiburan",
        [
            ("*://*.youtube.com/*", ("youtube",)),
            ("*://*.netflix.com/*", ("netflix",)),
            ("*://*.twitch.tv/*", ("twitch",)),
            ("*://*.spotify.com/*", ("spotify",)),
            ("*://*.vidio.com/*", ("vidio",)),
            ("*://*.bstation.tv/*", ("bstation",)),
        ],
    ),
)

ALL_CATEGORIES: tuple[SiteCategory, ...] = (
    AI_CHATBOT,
    SITUS_JAWABAN,
    GAME_ONLINE,
    MEDIA_SOSIAL,
    HIBURAN,
)

BY_KEY: dict[str, SiteCategory] = {c.key: c for c in ALL_CATEGORIES}

# Which categories each profile switches on by default.
PROFILE_DEFAULTS: dict[str, tuple[str, ...]] = {
    "exam": ("ai_chatbot", "situs_jawaban", "game_online", "media_sosial", "hiburan"),
    "lab": ("ai_chatbot", "game_online", "media_sosial"),
    "free": (),
}


def rules_for(keys: list[str] | tuple[str, ...]) -> tuple[SiteRule, ...]:
    """Collect the rules for the given category keys, de-duplicated by pattern."""
    seen: set[str] = set()
    out: list[SiteRule] = []
    for key in keys:
        category = BY_KEY.get(key)
        if category is None:
            continue
        for rule in category.rules:
            if rule.pattern not in seen:
                seen.add(rule.pattern)
                out.append(rule)
    return tuple(out)


def rules_for_profile(mode: str) -> tuple[SiteRule, ...]:
    return rules_for(PROFILE_DEFAULTS.get(mode, ()))


_HOST = re.compile(r"^([a-z0-9-]+\.)+[a-z0-9-]{2,}$")
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def custom_site(text: str) -> SiteRule | None:
    """A site the admin typed, as a blocklist rule; None if it is not one.

    Accepts what people paste from the address bar: ``tiktok.com``,
    ``www.tiktok.com``, ``https://www.tiktok.com/@someone/video/1``. Only
    the site is kept, so the whole site and its subdomains are blocked
    (Chrome/Edge URLBlocklist), not just the page that was pasted.
    """
    host = text.strip().lower()
    host = re.sub(r"^[a-z]+://", "", host)
    host = re.split(r"[/?#:]", host, maxsplit=1)[0]
    host = host.rstrip(".")  # a pasted FQDN may end with a dot (example.com.)
    for prefix in ("*.", "www."):
        host = host.removeprefix(prefix)
    if _IPV4.match(host):
        if not all(int(part) <= 255 for part in host.split(".")):
            return None
        return SiteRule(pattern=host, category=CUSTOM_SITE_CATEGORY)
    if len(host) > 253 or not _HOST.match(host):
        return None
    # For the window-title detector: the site's name, when it is distinctive.
    name = host.split(".")[-2]
    keywords = (name,) if len(name) >= 5 else ()
    return SiteRule(pattern=host, category=CUSTOM_SITE_CATEGORY, keywords=keywords)
