//! Whitelist matching with identity checks: an app is allowed only if its name
//! is whitelisted AND it passes the path/SHA-256 pinned to that rule, so a game
//! renamed to `chrome.exe`-style names is flagged as impersonation.

use super::identity::{HashCache, ProcessInfo};
use crate::rules::{AppRule, RuleSet};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Verdict {
    Allowed,
    NotWhitelisted,
    Impersonation,
}

#[derive(Clone, Debug, PartialEq)]
pub struct MatchResult {
    pub verdict: Verdict,
    pub reason: String,
}

impl MatchResult {
    pub fn is_violation(&self) -> bool {
        self.verdict != Verdict::Allowed
    }
}

#[derive(Default)]
pub struct WhitelistMatcher {
    hashes: HashCache,
}

impl WhitelistMatcher {
    pub fn check(&mut self, proc_: &ProcessInfo, ruleset: &RuleSet) -> MatchResult {
        let name = proc_.name.trim().to_lowercase();
        let result = |verdict, reason: String| MatchResult { verdict, reason };
        if !ruleset.effective_whitelist_names().contains(&name) {
            return result(Verdict::NotWhitelisted, format!("'{name}' not in whitelist"));
        }
        let rule: Option<&AppRule> = ruleset.app_whitelist.iter().find(|r| r.process_name == name);
        let Some(rule) = rule.filter(|r| r.expected_sha256.is_some() || r.expected_path_contains.is_some())
        else {
            return result(Verdict::Allowed, format!("'{name}' whitelisted by name"));
        };

        if let Some(fragment) = &rule.expected_path_contains {
            let path = proc_.exe_path.as_deref().unwrap_or("").replace('\\', "/").to_lowercase();
            if !path.contains(&fragment.replace('\\', "/").to_lowercase()) {
                return result(
                    Verdict::Impersonation,
                    format!(
                        "'{name}' path mismatch (expected to contain '{fragment}', got '{}')",
                        proc_.exe_path.as_deref().unwrap_or("None")
                    ),
                );
            }
        }

        if let Some(expected) = &rule.expected_sha256 {
            let Some(path) = proc_.exe_path.as_deref().filter(|p| !p.is_empty()) else {
                return result(Verdict::Impersonation, format!("'{name}' has no path to verify hash"));
            };
            let Some(actual) = self.hashes.sha256(path) else {
                return result(
                    Verdict::Impersonation,
                    format!("'{name}' could not be hashed for verification"),
                );
            };
            if &actual != expected {
                return result(
                    Verdict::Impersonation,
                    format!(
                        "'{name}' SHA-256 mismatch (expected {}…, got {}…)",
                        &expected[..12],
                        &actual[..12]
                    ),
                );
            }
        }
        result(Verdict::Allowed, format!("'{name}' verified"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::random_hex;

    fn proc_(name: &str, path: Option<&str>) -> ProcessInfo {
        ProcessInfo { pid: 100, name: name.into(), exe_path: path.map(str::to_string), ..Default::default() }
    }

    fn rules(app: Option<AppRule>) -> RuleSet {
        let mut rs: RuleSet = serde_json::from_str(r#"{"revision": 1, "dry_run": false}"#).unwrap();
        rs.app_whitelist.extend(app);
        rs
    }

    fn rule(name: &str, sha: Option<String>, path: Option<&str>) -> AppRule {
        AppRule {
            process_name: name.into(),
            display_name: String::new(),
            expected_sha256: sha,
            expected_path_contains: path.map(str::to_string),
        }
    }

    #[test]
    fn default_browser_allowed_and_others_flagged() {
        let mut m = WhitelistMatcher::default();
        assert!(!m.check(&proc_("chrome.exe", None), &rules(None)).is_violation());
        let r = m.check(&proc_("game.exe", None), &rules(None));
        assert_eq!(r.verdict, Verdict::NotWhitelisted);
    }

    #[test]
    fn explicit_and_path_pinned_rules() {
        let mut m = WhitelistMatcher::default();
        let rs = rules(Some(rule("code.exe", None, Some("Microsoft VS Code"))));
        assert!(!m
            .check(&proc_("code.exe", Some("C:\\Program Files\\Microsoft VS Code\\Code.exe")), &rs)
            .is_violation());
        let r = m.check(&proc_("code.exe", Some("C:\\Games\\code.exe")), &rs);
        assert_eq!(r.verdict, Verdict::Impersonation);
    }

    #[test]
    fn hash_pinned_rule_detects_a_renamed_game() {
        let dir = std::env::temp_dir().join(format!("ls-match-{}", random_hex(4)));
        std::fs::create_dir_all(&dir).unwrap();
        let real = dir.join("real.exe");
        let fake = dir.join("fake.exe");
        std::fs::write(&real, b"the real editor").unwrap();
        std::fs::write(&fake, b"a game").unwrap();
        let digest = HashCache::default().sha256(real.to_str().unwrap()).unwrap();
        let rs = rules(Some(rule("code.exe", Some(digest), None)));
        let mut m = WhitelistMatcher::default();
        assert!(!m.check(&proc_("code.exe", real.to_str()), &rs).is_violation());
        assert_eq!(m.check(&proc_("code.exe", fake.to_str()), &rs).verdict, Verdict::Impersonation);
        assert_eq!(m.check(&proc_("code.exe", None), &rs).verdict, Verdict::Impersonation);
        let _ = std::fs::remove_dir_all(dir);
    }
}
