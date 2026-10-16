//! The safety gate: deny by default, decided before any whitelist matching.
//!
//! A process is only *evaluable* if every condition holds; anything uncertain
//! (unknown owner, other session, no window, protected name, the agent itself,
//! an elevated program) is left completely alone. Ported one-to-one from
//! `agent/labsentinel_agent/enforce/safety.py`, tests included.

use std::collections::HashSet;

use super::identity::ProcessInfo;
use crate::rules::PROTECTED_PROCESSES;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SafetyDecision {
    pub evaluable: bool,
    pub reason: &'static str,
}

pub struct SafetyPolicy {
    protected: HashSet<String>,
    own_pids: HashSet<u32>,
}

impl SafetyPolicy {
    /// `extra_protected` may add to, never remove from, the hardcoded floor.
    pub fn new(own_pids: &[u32], extra_protected: &[&str]) -> Self {
        let mut protected: HashSet<String> = PROTECTED_PROCESSES.iter().map(|s| s.to_string()).collect();
        protected.extend(extra_protected.iter().map(|s| s.trim().to_lowercase()));
        Self { protected, own_pids: own_pids.iter().copied().collect() }
    }

    pub fn evaluate(&self, proc_: &ProcessInfo) -> SafetyDecision {
        let deny = |reason| SafetyDecision { evaluable: false, reason };
        if self.own_pids.contains(&proc_.pid) {
            return deny("agent's own process");
        }
        if proc_.pid <= 4 {
            return deny("core system pid");
        }
        let name = proc_.name.trim().to_lowercase();
        if name.is_empty() {
            return deny("unknown process name");
        }
        if self.protected.contains(&name) {
            return deny("protected process");
        }
        if proc_.is_system_owned() {
            return deny("system-owned process");
        }
        if !proc_.is_current_user {
            return deny("not owned by interactive user");
        }
        if proc_.elevated {
            return deny("started with administrator rights");
        }
        if !proc_.has_visible_window {
            return deny("no visible window");
        }
        SafetyDecision { evaluable: true, reason: "user-facing, non-protected" }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn user_app(name: &str) -> ProcessInfo {
        ProcessInfo {
            pid: 4321,
            name: name.into(),
            exe_path: Some(format!("C:\\Games\\{name}")),
            username: Some("LAB\\andi".into()),
            session_id: Some(1),
            has_visible_window: true,
            window_title: "Game".into(),
            is_current_user: true,
            elevated: false,
        }
    }

    fn policy() -> SafetyPolicy {
        SafetyPolicy::new(&[999], &[])
    }

    #[test]
    fn ordinary_user_app_is_evaluable() {
        assert!(policy().evaluate(&user_app("game.exe")).evaluable);
    }

    #[test]
    fn protected_processes_are_never_evaluable() {
        for name in PROTECTED_PROCESSES {
            assert!(!policy().evaluate(&user_app(name)).evaluable, "{name}");
            assert!(!policy().evaluate(&user_app(&name.to_uppercase())).evaluable, "{name}");
        }
    }

    #[test]
    fn system_owned_is_never_evaluable() {
        for account in ["SYSTEM", "NT AUTHORITY\\SYSTEM", "LOCAL SERVICE", "network service", ""] {
            let mut p = user_app("game.exe");
            p.username = Some(account.into());
            assert!(!policy().evaluate(&p).evaluable, "{account}");
        }
    }

    #[test]
    fn what_an_administrator_started_is_never_evaluable() {
        let mut p = user_app("setup.exe");
        p.elevated = true;
        assert!(!policy().evaluate(&p).evaluable);
    }

    #[test]
    fn a_renamed_game_is_still_evaluable() {
        assert!(policy().evaluate(&user_app("notepad_game.exe")).evaluable);
    }

    #[test]
    fn own_and_core_pids_are_protected() {
        let mut p = user_app("labsentinel-test.exe");
        p.pid = 999;
        assert!(!policy().evaluate(&p).evaluable);
        for pid in [0, 4] {
            let mut p = user_app("game.exe");
            p.pid = pid;
            assert!(!policy().evaluate(&p).evaluable);
        }
    }

    #[test]
    fn other_users_windowless_and_unnamed_are_protected() {
        let mut p = user_app("game.exe");
        p.is_current_user = false;
        assert!(!policy().evaluate(&p).evaluable);
        let mut p = user_app("game.exe");
        p.has_visible_window = false;
        assert!(!policy().evaluate(&p).evaluable);
        assert!(!policy().evaluate(&user_app("  ")).evaluable);
    }

    #[test]
    fn extra_protected_adds_but_never_removes() {
        let p = SafetyPolicy::new(&[], &["Game.exe"]);
        assert!(!p.evaluate(&user_app("game.exe")).evaluable);
        assert!(!p.evaluate(&user_app("explorer.exe")).evaluable);
    }
}
