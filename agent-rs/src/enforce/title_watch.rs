//! Supplementary site detection from the foreground browser's window title.
//!
//! The browser policy does the real blocking; this only *records* a suspected
//! visit, clearly labelled as coming from a window title (a weak signal). Same
//! token rules as `agent/labsentinel_agent/enforce/title_watch.py`.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use tokio::time::Instant;

use crate::rules::{RuleSet, SiteRule};

/// Only browsers in the foreground are considered.
pub const BROWSER_PROCESSES: &[&str] = &["chrome.exe", "msedge.exe"];

const TITLE_SUFFIXES: &[&str] = &[
    " - google chrome",
    " - microsoft edge",
    " and 1 more page - personal - microsoft edge",
];

/// Tokens that must never trigger a match on their own.
const NOISE: &[&str] = &[
    "www", "com", "net", "org", "co", "id", "io", "ac", "ai", "tv", "me", "gg", "ly", "cc", "uk",
    "us", "info", "biz", "edu", "gov", "site", "online", "xyz", "html", "php", "google",
    "microsoft", "apple", "amazon", "yahoo", "app", "web", "chat", "mail", "play", "my", "api",
    "cdn", "static", "docs", "drive", "login", "account", "home", "news", "video", "music",
    "live", "store", "shop", "support", "help", "blog", "forum", "wiki", "search", "image",
    "images", "portal", "student", "class",
];

const MIN_TOKEN_LEN: usize = 2;

#[derive(Clone, Debug, PartialEq)]
pub struct TitleHit {
    pub title: String,
    pub matched: String,
    pub category: String,
}

/// `*://*.facebook.com/*` -> {"facebook"}; generic parts are dropped.
pub fn domain_tokens(pattern: &str) -> HashSet<String> {
    let mut p = pattern.trim().to_lowercase();
    // Strip a scheme like "https://" or the wildcard "*://".
    if let Some(idx) = p.find("://") {
        if p[..idx].chars().all(|c| c.is_ascii_alphabetic() || c == '*') && idx > 0 {
            p = p[idx + 3..].to_string();
        }
    }
    let p = p.split('/').next().unwrap_or("");
    let p = p.trim_start_matches(['*', '.']).trim_matches('*');
    let p = p.split(':').next().unwrap_or("");
    p.split('.')
        .filter(|t| t.chars().count() >= MIN_TOKEN_LEN && !NOISE.contains(t))
        .map(str::to_string)
        .collect()
}

pub fn normalize_title(title: &str) -> String {
    let mut t = title.trim().to_lowercase();
    for suffix in TITLE_SUFFIXES {
        if let Some(stripped) = t.strip_suffix(suffix) {
            t = stripped.to_string();
            break;
        }
    }
    t.trim().to_string()
}

/// The first rule keyword or domain token found in the title.
pub fn match_title(title: &str, rules: &[SiteRule]) -> Option<TitleHit> {
    let clean = normalize_title(title);
    if clean.is_empty() {
        return None;
    }
    for rule in rules {
        let hit = |matched: &str| TitleHit {
            title: title.to_string(),
            matched: matched.to_string(),
            category: rule.category.clone(),
        };
        if let Some(k) = rule.keywords.iter().find(|k| !k.is_empty() && clean.contains(k.as_str())) {
            return Some(hit(k));
        }
        // Deterministic order, so the same title always reports the same token.
        let mut tokens: Vec<String> = domain_tokens(&rule.pattern).into_iter().collect();
        tokens.sort();
        if let Some(t) = tokens.iter().find(|t| clean.contains(t.as_str())) {
            return Some(hit(t));
        }
    }
    None
}

/// Decides, tick by tick, whether the foreground window is a reportable hit.
pub struct TitleWatcher {
    dedup_window: Duration,
    recent: HashMap<String, Instant>,
}

impl Default for TitleWatcher {
    fn default() -> Self {
        Self { dedup_window: Duration::from_secs(120), recent: HashMap::new() }
    }
}

impl TitleWatcher {
    /// One check of the foreground window. `enabled` is false while paused.
    pub fn check(&mut self, enabled: bool, ruleset: Option<&RuleSet>, process: &str, title: &str) -> Option<TitleHit> {
        if !enabled {
            return None;
        }
        let ruleset = ruleset.filter(|r| !r.site_blocklist.is_empty())?;
        if !BROWSER_PROCESSES.contains(&process.trim().to_lowercase().as_str()) {
            return None;
        }
        let hit = match_title(title, &ruleset.site_blocklist)?;
        if self.is_duplicate(normalize_title(title)) {
            return None;
        }
        Some(hit)
    }

    fn is_duplicate(&mut self, key: String) -> bool {
        let now = Instant::now();
        self.recent.retain(|_, seen| now.duration_since(*seen) <= self.dedup_window);
        if self.recent.contains_key(&key) {
            return true;
        }
        self.recent.insert(key, now);
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn site(pattern: &str, keywords: &[&str]) -> SiteRule {
        SiteRule {
            pattern: pattern.into(),
            category: "game".into(),
            keywords: keywords.iter().map(|s| s.to_string()).collect(),
        }
    }

    fn rules(sites: Vec<SiteRule>) -> RuleSet {
        let mut rs: RuleSet = serde_json::from_str(r#"{"revision":1}"#).unwrap();
        rs.site_blocklist = sites;
        rs
    }

    #[test]
    fn domain_tokens_strip_scheme_wildcards_and_noise() {
        let set = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<HashSet<_>>();
        assert_eq!(domain_tokens("*://*.facebook.com/*"), set(&["facebook"]));
        assert_eq!(domain_tokens("https://www.roblox.com/games"), set(&["roblox"]));
        assert_eq!(domain_tokens("chat.openai.com"), set(&["openai"]));
        assert_eq!(domain_tokens("gemini.google.com"), set(&["gemini"]));
        assert!(domain_tokens("x.com").is_empty());
    }

    #[test]
    fn normalize_strips_the_browser_suffix() {
        assert_eq!(normalize_title("Roblox - Google Chrome"), "roblox");
        assert_eq!(normalize_title("  Tugas - Microsoft Edge "), "tugas");
    }

    #[test]
    fn matches_keywords_and_domain_tokens() {
        let r = vec![site("tiktok.com", &["tiktok"]), site("*://*.facebook.com/*", &[])];
        assert_eq!(match_title("TikTok - Make Your Day - Google Chrome", &r).unwrap().matched, "tiktok");
        assert_eq!(match_title("(3) Facebook - Google Chrome", &r).unwrap().matched, "facebook");
        assert!(match_title("Google Classroom - Google Chrome", &r).is_none());
        assert!(match_title("Google Chrome", &[site("google.com", &[])]).is_none());
        assert!(match_title("", &r).is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn reports_browser_hits_once_per_window() {
        let rs = rules(vec![site("roblox.com", &[])]);
        let mut w = TitleWatcher::default();
        assert!(w.check(true, Some(&rs), "notepad.exe", "roblox notes").is_none());
        assert!(w.check(false, Some(&rs), "chrome.exe", "Roblox - Google Chrome").is_none());
        assert!(w.check(true, Some(&rs), "chrome.exe", "Roblox - Google Chrome").is_some());
        assert!(w.check(true, Some(&rs), "chrome.exe", "Roblox - Google Chrome").is_none(), "deduplicated");
        assert!(w.check(true, Some(&rs), "msedge.exe", "Roblox Studio - Microsoft Edge").is_some());
        tokio::time::advance(Duration::from_secs(121)).await;
        assert!(w.check(true, Some(&rs), "chrome.exe", "Roblox - Google Chrome").is_some(), "dedup expired");
        assert!(w.check(true, None, "chrome.exe", "Roblox").is_none());
    }
}
