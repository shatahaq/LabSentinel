//! Crash-safe cache of the last ruleset, so the agent keeps enforcing while
//! disconnected and restores the rules immediately on restart.
//! Same file and JSON as the Python agent (`ruleset.json`).

use std::path::PathBuf;

use crate::rules::RuleSet;
use crate::util::atomic_write;

pub struct RuleCache {
    path: PathBuf,
    current: Option<RuleSet>,
}

impl RuleCache {
    pub fn new(path: PathBuf) -> Self {
        Self { path, current: None }
    }

    pub fn current(&self) -> Option<&RuleSet> {
        self.current.as_ref()
    }

    pub fn revision(&self) -> i64 {
        self.current.as_ref().map_or(-1, |r| r.revision)
    }

    /// Load the persisted ruleset; `None` if absent or corrupt.
    pub fn load(&mut self) -> Option<RuleSet> {
        self.current = match std::fs::read_to_string(&self.path) {
            Ok(text) => match serde_json::from_str::<RuleSet>(&text)
                .map_err(|e| e.to_string())
                .and_then(RuleSet::validated)
            {
                Ok(rs) => {
                    log::info!("loaded cached ruleset revision {}", rs.revision);
                    Some(rs)
                }
                Err(e) => {
                    log::warn!("cached ruleset was corrupt; ignoring ({e})");
                    None
                }
            },
            Err(_) => None,
        };
        self.current.clone()
    }

    /// Persist a newer ruleset; older revisions are ignored. True if updated.
    pub fn update(&mut self, ruleset: &RuleSet) -> bool {
        if let Some(have) = &self.current {
            if ruleset.revision < have.revision {
                log::debug!("ignoring stale ruleset rev {} (have {})", ruleset.revision, have.revision);
                return false;
            }
        }
        let json = serde_json::to_vec(ruleset).expect("ruleset serialises");
        if let Err(e) = atomic_write(&self.path, &json) {
            // Still apply it in memory: enforcement must not wait for the disk.
            log::warn!("could not persist the ruleset: {e}");
        }
        self.current = Some(ruleset.clone());
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::random_hex;

    fn rs(revision: i64) -> RuleSet {
        serde_json::from_str(&format!(r#"{{"revision": {revision}}}"#)).unwrap()
    }

    #[test]
    fn keeps_newest_and_survives_restart() {
        let path = std::env::temp_dir().join(format!("ls-cache-{}", random_hex(4))).join("ruleset.json");
        let mut cache = RuleCache::new(path.clone());
        assert!(cache.load().is_none());
        assert!(cache.update(&rs(3)));
        assert!(!cache.update(&rs(2)), "stale revision ignored");
        assert!(cache.update(&rs(3)), "same revision re-applied");
        let mut again = RuleCache::new(path.clone());
        assert_eq!(again.load().unwrap().revision, 3);
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }

    #[test]
    fn corrupt_cache_is_ignored() {
        let path = std::env::temp_dir().join(format!("ls-cache-{}", random_hex(4))).join("ruleset.json");
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(&path, "{broken").unwrap();
        assert!(RuleCache::new(path.clone()).load().is_none());
        let _ = std::fs::remove_dir_all(path.parent().unwrap());
    }
}
