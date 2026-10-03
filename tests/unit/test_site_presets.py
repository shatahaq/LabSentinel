"""Site category preset tests."""

from __future__ import annotations

from labsentinel_agent.enforce.title_watch import domain_tokens, match_title
from labsentinel_dashboard.domain.site_presets import (
    ALL_CATEGORIES,
    BY_KEY,
    PROFILE_DEFAULTS,
    rules_for,
    rules_for_profile,
)


def test_categories_have_unique_keys_and_content() -> None:
    keys = [c.key for c in ALL_CATEGORIES]
    assert len(keys) == len(set(keys))
    for c in ALL_CATEGORIES:
        assert c.rules and c.label and c.description


# Domains too short/generic to match safely in a window title. These are still
# blocked by browser policy; only the supplementary title detection skips them.
TITLE_UNDETECTABLE = {"*://*.x.com/*"}


def test_patterns_yield_usable_title_tokens_except_documented_ones() -> None:
    """A pattern with no tokens can never match a title — keep that list honest."""
    tokenless = {
        rule.pattern
        for category in ALL_CATEGORIES
        for rule in category.rules
        if not domain_tokens(rule.pattern)
    }
    assert tokenless == TITLE_UNDETECTABLE


def test_generic_tokens_never_match_alone() -> None:
    """'google'/'chat' must not be matchable, or ordinary pages get flagged."""
    for pattern, forbidden in [
        ("*://gemini.google.com/*", "google"),
        ("*://chat.openai.com/*", "chat"),
        ("*://copilot.microsoft.com/*", "microsoft"),
        ("*://web.whatsapp.com/*", "web"),
    ]:
        assert forbidden not in domain_tokens(pattern)


def test_rules_for_dedups_across_categories() -> None:
    combined = rules_for(["ai_chatbot", "ai_chatbot", "game_online"])
    patterns = [r.pattern for r in combined]
    assert len(patterns) == len(set(patterns))


def test_unknown_category_ignored() -> None:
    assert rules_for(["tidak_ada"]) == ()


def test_exam_profile_is_strictest() -> None:
    exam = rules_for_profile("exam")
    lab = rules_for_profile("lab")
    assert len(exam) > len(lab) > 0
    assert rules_for_profile("free") == ()


def test_exam_covers_every_category() -> None:
    assert set(PROFILE_DEFAULTS["exam"]) == set(BY_KEY)


def test_presets_match_realistic_titles() -> None:
    rules = rules_for_profile("exam")
    for title, expected in [
        ("ChatGPT - Google Chrome", "AI chatbot"),
        ("Roblox - Play Together - Google Chrome", "game online"),
        ("Instagram - Microsoft Edge", "media sosial"),
        ("Chegg Study - Google Chrome", "situs jawaban"),
    ]:
        hit = match_title(title, rules)
        assert hit is not None and hit.category == expected, title


def test_ordinary_coursework_title_is_not_flagged() -> None:
    rules = rules_for_profile("exam")
    for title in (
        "Modul Praktikum Basis Data - Google Chrome",
        "Google Classroom - Google Chrome",
        "Laporan Akhir.docx - Word",
    ):
        assert match_title(title, rules) is None, title


def test_to_dict_shape_for_ui() -> None:
    d = BY_KEY["ai_chatbot"].to_dict()
    assert d["key"] == "ai_chatbot"
    assert d["count"] == len(BY_KEY["ai_chatbot"].rules)
    assert isinstance(d["patterns"], list)
