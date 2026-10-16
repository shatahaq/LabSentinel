//! The ruleset the dashboard pushes: whitelist, site blocklist, active profile.
//!
//! Mirrors `shared/labsentinel_shared/rules.py`. The JSON shape is identical,
//! so a ruleset cached by this agent can be read by the Python agent and the
//! other way around.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

/// Operating profile chosen by the supervisor.
#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum EnforcementMode {
    /// Ujian: browsers only, strictest site blocking.
    Exam,
    /// Praktikum: browsers + coursework apps.
    #[default]
    Lab,
    /// Bebas: watch only.
    Free,
}

/// Process names the agent must never touch, whatever the configuration.
pub const PROTECTED_PROCESSES: &[&str] = &[
    // core OS
    "system",
    "system idle process",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "lsaiso.exe",
    "svchost.exe",
    "fontdrvhost.exe",
    "dwm.exe",
    "sihost.exe",
    "taskhostw.exe",
    "ctfmon.exe",
    "spoolsv.exe",
    "explorer.exe",
    "runtimebroker.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "applicationframehost.exe",
    "textinputhost.exe",
    "useroobebroker.exe",
    "dllhost.exe",
    "conhost.exe",
    "audiodg.exe",
    "wudfhost.exe",
    // security / management agents commonly present in labs
    "msmpeng.exe",
    "nissrv.exe",
    "securityhealthservice.exe",
    "securityhealthsystray.exe",
    "mpdefendercoreservice.exe",
    // Lab Sentinel's own binaries
    "labsentinel-agent.exe",
    "labsentinel-service.exe",
    "labsentinel-tray.exe",
    "labsentinel-watchdog.exe",
];

/// Category of sites a supervisor typed in by hand.
pub const CUSTOM_SITE_CATEGORY: &str = "kustom";

/// Browsers that are always allowed.
pub const DEFAULT_WHITELIST: &[&str] = &["chrome.exe", "msedge.exe", "explorer.exe"];

fn default_category() -> String {
    "lainnya".to_string()
}
fn default_true() -> bool {
    true
}
fn default_grace() -> i64 {
    5
}

/// One allowed application, optionally pinned to a verified identity.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct AppRule {
    pub process_name: String,
    #[serde(default)]
    pub display_name: String,
    #[serde(default)]
    pub expected_sha256: Option<String>,
    #[serde(default)]
    pub expected_path_contains: Option<String>,
}

/// One blocked site: a Chrome/Edge URLBlocklist pattern plus title keywords.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct SiteRule {
    pub pattern: String,
    #[serde(default = "default_category")]
    pub category: String,
    #[serde(default)]
    pub keywords: Vec<String>,
}

/// The complete, versioned ruleset handed to an agent.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct RuleSet {
    pub revision: i64,
    #[serde(default)]
    pub mode: EnforcementMode,
    #[serde(default = "default_true")]
    pub dry_run: bool,
    #[serde(default = "default_grace")]
    pub close_grace_seconds: i64,
    #[serde(default)]
    pub app_whitelist: Vec<AppRule>,
    #[serde(default)]
    pub site_blocklist: Vec<SiteRule>,
    #[serde(default = "default_true")]
    pub block_incognito: bool,
}

impl RuleSet {
    /// Check the limits the Python model enforces and normalise names, so an
    /// out-of-range or hostile ruleset is refused instead of acted on.
    pub fn validated(mut self) -> Result<Self, String> {
        if self.revision < 0 {
            return Err("revision must be >= 0".into());
        }
        if !(0..=60).contains(&self.close_grace_seconds) {
            return Err("close_grace_seconds must be 0..60".into());
        }
        for rule in &mut self.app_whitelist {
            rule.process_name = rule.process_name.trim().to_lowercase();
            if rule.process_name.is_empty() || rule.process_name.chars().count() > 260 {
                return Err("app process_name must be 1..260 characters".into());
            }
            if let Some(hash) = &rule.expected_sha256 {
                let hash = hash.trim().to_lowercase();
                if hash.len() != 64 || !hash.chars().all(|c| c.is_ascii_hexdigit()) {
                    return Err("expected_sha256 must be 64 hex characters".into());
                }
                rule.expected_sha256 = Some(hash);
            }
        }
        for rule in &mut self.site_blocklist {
            if rule.pattern.is_empty() || rule.pattern.chars().count() > 512 {
                return Err("site pattern must be 1..512 characters".into());
            }
            rule.keywords = rule
                .keywords
                .iter()
                .map(|k| k.trim().to_lowercase())
                .filter(|k| !k.is_empty())
                .collect();
        }
        Ok(self)
    }

    /// Allowed process names, always including the immovable defaults.
    /// Ujian allows the browsers only; the coursework apps stay saved for Praktikum.
    pub fn effective_whitelist_names(&self) -> HashSet<String> {
        let mut names: HashSet<String> = DEFAULT_WHITELIST.iter().map(|s| s.to_string()).collect();
        if self.mode != EnforcementMode::Exam {
            names.extend(self.app_whitelist.iter().map(|r| r.process_name.clone()));
        }
        names
    }

    /// Whether apps are closed and sites blocked, or only recorded.
    pub fn acts(&self) -> bool {
        !self.dry_run && self.mode != EnforcementMode::Free
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ruleset(mode: EnforcementMode, dry_run: bool) -> RuleSet {
        RuleSet {
            revision: 1,
            mode,
            dry_run,
            close_grace_seconds: 5,
            app_whitelist: vec![AppRule {
                process_name: "code.exe".into(),
                display_name: String::new(),
                expected_sha256: None,
                expected_path_contains: None,
            }],
            site_blocklist: vec![],
            block_incognito: true,
        }
    }

    #[test]
    fn exam_allows_only_the_browsers() {
        let names = ruleset(EnforcementMode::Exam, false).effective_whitelist_names();
        assert!(names.contains("chrome.exe"));
        assert!(!names.contains("code.exe"));
        let names = ruleset(EnforcementMode::Lab, false).effective_whitelist_names();
        assert!(names.contains("code.exe"));
    }

    #[test]
    fn acts_only_when_enforcing_outside_free_mode() {
        assert!(ruleset(EnforcementMode::Lab, false).acts());
        assert!(!ruleset(EnforcementMode::Lab, true).acts());
        assert!(!ruleset(EnforcementMode::Free, false).acts());
    }

    #[test]
    fn parses_the_dashboard_json_and_normalises() {
        let json = r#"{"revision": 3, "mode": "exam", "dry_run": false,
            "close_grace_seconds": 5,
            "app_whitelist": [{"process_name": " Code.EXE ", "display_name": "",
                "expected_sha256": null, "expected_path_contains": null}],
            "site_blocklist": [{"pattern": "tiktok.com", "category": "kustom",
                "keywords": [" TikTok ", ""]}],
            "block_incognito": true}"#;
        let rs: RuleSet = serde_json::from_str(json).unwrap();
        let rs = rs.validated().unwrap();
        assert_eq!(rs.mode, EnforcementMode::Exam);
        assert_eq!(rs.app_whitelist[0].process_name, "code.exe");
        assert_eq!(rs.site_blocklist[0].keywords, vec!["tiktok".to_string()]);
    }

    #[test]
    fn missing_fields_take_the_python_defaults() {
        let rs: RuleSet = serde_json::from_str(r#"{"revision": 0}"#).unwrap();
        assert_eq!(rs.mode, EnforcementMode::Lab);
        assert!(rs.dry_run);
        assert_eq!(rs.close_grace_seconds, 5);
        assert!(rs.block_incognito);
    }

    #[test]
    fn rejects_out_of_range_values() {
        let mut rs = ruleset(EnforcementMode::Lab, false);
        rs.close_grace_seconds = 99;
        assert!(rs.validated().is_err());
        let mut rs = ruleset(EnforcementMode::Lab, false);
        rs.app_whitelist[0].expected_sha256 = Some("abc".into());
        assert!(rs.validated().is_err());
    }
}
