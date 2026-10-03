"""Ready-made blocked-site categories the admin can switch on with one click.

Patterns use the Chrome/Edge ``URLBlocklist`` syntax. Keywords feed the
supplementary window-title detector, so they are written as they would appear in
a page title rather than as domains.

These lists are a starting point, not an exhaustive filter: new sites appear
constantly, which is exactly why the application whitelist — not this list — is
the primary safeguard. Admins can edit, extend, import and export them.
"""

from __future__ import annotations

from dataclasses import dataclass

from labsentinel_shared.rules import SiteRule


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
