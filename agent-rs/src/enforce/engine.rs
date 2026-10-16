//! Enforcement engine: safety gate → whitelist match → evidence → act.
//!
//! On a violation it captures a screenshot, then either acts (warn the
//! participant, wait the grace period, close the windows, kill only if the
//! program will not close) or, in dry run, only records what it would have
//! done. The same pid is handled at most once per dedup window.

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::mpsc;
use tokio::time::{sleep, Instant};

use super::identity::ProcessInfo;
use super::matcher::{Verdict, WhitelistMatcher};
use super::safety::SafetyPolicy;
use super::{Desktop, ProcessControl, Warner};
use crate::protocol::{AgentState, ViolationKind};
use crate::rules::RuleSet;
use crate::util::now_ts;

/// Emitted on every violation (acted or dry run); the agent ships it.
#[derive(Clone, Debug, PartialEq)]
pub struct ViolationRecord {
    pub kind: ViolationKind,
    pub subject: String,
    pub detail: String,
    pub pid: u32,
    pub dry_run: bool,
    pub acted: bool,
    pub occurred_at: f64,
    pub screenshot_jpeg: Option<Vec<u8>>,
}

pub type StateFn = Arc<dyn Fn() -> AgentState + Send + Sync>;
pub type RulesFn = Arc<dyn Fn() -> Option<RuleSet> + Send + Sync>;

pub struct Engine {
    pub safety: SafetyPolicy,
    pub matcher: Mutex<WhitelistMatcher>,
    pub control: Arc<dyn ProcessControl>,
    pub desktop: Arc<dyn Desktop>,
    pub warner: Arc<dyn Warner>,
    pub emit: mpsc::UnboundedSender<ViolationRecord>,
    pub state: StateFn,
    pub ruleset: RulesFn,
    pub dedup_window: Duration,
    pub kill_timeout: Duration,
    recent: Mutex<HashMap<u32, Instant>>,
}

impl Engine {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        safety: SafetyPolicy,
        control: Arc<dyn ProcessControl>,
        desktop: Arc<dyn Desktop>,
        warner: Arc<dyn Warner>,
        emit: mpsc::UnboundedSender<ViolationRecord>,
        state: StateFn,
        ruleset: RulesFn,
    ) -> Self {
        Self {
            safety,
            matcher: Mutex::new(WhitelistMatcher::default()),
            control,
            desktop,
            warner,
            emit,
            state,
            ruleset,
            dedup_window: Duration::from_secs(20),
            kill_timeout: Duration::from_secs(3),
            recent: Mutex::new(HashMap::new()),
        }
    }

    /// Evaluate one process and enforce if warranted. Returns the record.
    pub async fn handle_process(&self, proc_: &ProcessInfo) -> Option<ViolationRecord> {
        let state = (self.state)();
        if state == AgentState::Paused {
            return None; // paused: enforce nothing, capture nothing
        }
        let ruleset = (self.ruleset)()?; // no rules yet: do nothing
        let safety = self.safety.evaluate(proc_);
        if !safety.evaluable {
            log::debug!("skip pid={} name={}: {}", proc_.pid, proc_.name, safety.reason);
            return None;
        }
        let matched = self.matcher.lock().unwrap().check(proc_, &ruleset);
        if !matched.is_violation() || self.is_duplicate(proc_.pid) {
            return None;
        }
        let dry_run = !ruleset.acts() || state == AgentState::DryRun;
        let kind = if matched.verdict == Verdict::Impersonation {
            ViolationKind::Impersonation
        } else {
            ViolationKind::AppBlocked
        };

        let screenshot = self.desktop.capture_jpeg(1280, 60).await;
        let mut acted = false;
        if dry_run {
            log::info!("[dry-run] would close pid={} name={} ({})", proc_.pid, proc_.name, matched.reason);
        } else {
            acted = self.close_gracefully(proc_, ruleset.close_grace_seconds as u32, &matched.reason).await;
        }
        let record = ViolationRecord {
            kind: if acted { ViolationKind::AppClosed } else { kind },
            subject: proc_.name.clone(),
            detail: matched.reason,
            pid: proc_.pid,
            dry_run,
            acted,
            occurred_at: now_ts(),
            screenshot_jpeg: screenshot,
        };
        let _ = self.emit.send(record.clone());
        Some(record)
    }

    async fn close_gracefully(&self, proc_: &ProcessInfo, grace: u32, reason: &str) -> bool {
        // 1. Warn the participant transparently. 2. Give them the grace period.
        self.warner.warn_closing(&proc_.name, grace, reason);
        if grace > 0 {
            sleep(Duration::from_secs(grace as u64)).await;
        }
        // 3. They closed it themselves: done.
        if !self.control.is_running(proc_.pid) {
            return true;
        }
        // 4. Ask its windows to close, then kill only if it will not.
        self.desktop.close_windows(proc_.pid).await;
        if self.wait_gone(proc_.pid, self.kill_timeout).await {
            return true;
        }
        log::warn!("force-killing pid={} name={} (did not close)", proc_.pid, proc_.name);
        let control = self.control.clone();
        let pid = proc_.pid;
        let killed = tokio::task::spawn_blocking(move || control.kill(pid)).await.unwrap_or(false);
        killed || self.wait_gone(proc_.pid, Duration::from_secs(2)).await
    }

    async fn wait_gone(&self, pid: u32, within: Duration) -> bool {
        let deadline = Instant::now() + within;
        while Instant::now() < deadline {
            if !self.control.is_running(pid) {
                return true;
            }
            sleep(Duration::from_millis(100)).await;
        }
        !self.control.is_running(pid)
    }

    fn is_duplicate(&self, pid: u32) -> bool {
        let now = Instant::now();
        let mut recent = self.recent.lock().unwrap();
        recent.retain(|_, seen| now.duration_since(*seen) <= self.dedup_window);
        if recent.contains_key(&pid) {
            return true;
        }
        recent.insert(pid, now);
        false
    }
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use crate::enforce::BoxFut;
    use std::collections::HashSet;
    use std::sync::atomic::{AtomicBool, Ordering};

    /// A fake desktop + process table for engine tests.
    #[derive(Default)]
    pub struct Fakes {
        pub running: Mutex<HashSet<u32>>,
        pub closes_on_request: AtomicBool,
        pub close_requests: Mutex<Vec<u32>>,
        pub kills: Mutex<Vec<u32>>,
        pub warnings: Mutex<Vec<String>>,
        pub captures: Mutex<u32>,
    }

    impl Desktop for Fakes {
        fn window_index(&self) -> BoxFut<'_, Option<HashMap<u32, String>>> {
            Box::pin(async { Some(HashMap::new()) })
        }
        fn foreground(&self) -> BoxFut<'_, (String, String)> {
            Box::pin(async { (String::new(), String::new()) })
        }
        fn capture_jpeg(&self, _w: u32, _q: u8) -> BoxFut<'_, Option<Vec<u8>>> {
            *self.captures.lock().unwrap() += 1;
            Box::pin(async { Some(vec![0xFF, 0xD8, 0xFF, 0xD9]) })
        }
        fn close_windows(&self, pid: u32) -> BoxFut<'_, bool> {
            self.close_requests.lock().unwrap().push(pid);
            if self.closes_on_request.load(Ordering::SeqCst) {
                self.running.lock().unwrap().remove(&pid);
            }
            Box::pin(async { true })
        }
        fn lock(&self) -> BoxFut<'_, bool> {
            Box::pin(async { true })
        }
        fn freeze(&self, _on: bool) -> BoxFut<'_, bool> {
            Box::pin(async { true })
        }
        fn available(&self) -> bool {
            true
        }
    }

    impl ProcessControl for Fakes {
        fn is_running(&self, pid: u32) -> bool {
            self.running.lock().unwrap().contains(&pid)
        }
        fn kill(&self, pid: u32) -> bool {
            self.kills.lock().unwrap().push(pid);
            self.running.lock().unwrap().remove(&pid);
            true
        }
    }

    impl Warner for Fakes {
        fn warn_closing(&self, app: &str, seconds: u32, _reason: &str) {
            self.warnings.lock().unwrap().push(format!("{app}:{seconds}"));
        }
        fn notify(&self, _t: &str, _m: &str) {}
    }

    pub fn game(pid: u32) -> ProcessInfo {
        ProcessInfo {
            pid,
            name: "game.exe".into(),
            exe_path: Some("C:\\Games\\game.exe".into()),
            username: Some("LAB\\andi".into()),
            session_id: Some(1),
            has_visible_window: true,
            window_title: "Game".into(),
            is_current_user: true,
            elevated: false,
        }
    }

    pub fn rules(json: &str) -> RuleSet {
        serde_json::from_str::<RuleSet>(json).unwrap().validated().unwrap()
    }

    fn engine(
        fakes: &Arc<Fakes>,
        state: AgentState,
        ruleset: Option<RuleSet>,
    ) -> (Engine, mpsc::UnboundedReceiver<ViolationRecord>) {
        let (tx, rx) = mpsc::unbounded_channel();
        let e = Engine::new(
            SafetyPolicy::new(&[], &[]),
            fakes.clone(),
            fakes.clone(),
            fakes.clone(),
            tx,
            Arc::new(move || state),
            Arc::new(move || ruleset.clone()),
        );
        (e, rx)
    }

    const ENFORCE: &str = r#"{"revision":1,"mode":"lab","dry_run":false,"close_grace_seconds":5}"#;

    #[tokio::test(start_paused = true)]
    async fn enforcing_closes_gracefully() {
        let fakes = Arc::new(Fakes::default());
        fakes.running.lock().unwrap().insert(50);
        fakes.closes_on_request.store(true, Ordering::SeqCst);
        let (e, mut rx) = engine(&fakes, AgentState::Enforcing, Some(rules(ENFORCE)));
        let rec = e.handle_process(&game(50)).await.unwrap();
        assert!(rec.acted);
        assert_eq!(rec.kind, ViolationKind::AppClosed);
        assert_eq!(*fakes.warnings.lock().unwrap(), vec!["game.exe:5".to_string()]);
        assert!(fakes.kills.lock().unwrap().is_empty());
        assert_eq!(rx.recv().await.unwrap().pid, 50);
    }

    #[tokio::test(start_paused = true)]
    async fn force_kill_when_graceful_close_fails() {
        let fakes = Arc::new(Fakes::default());
        fakes.running.lock().unwrap().insert(51);
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rules(ENFORCE)));
        let rec = e.handle_process(&game(51)).await.unwrap();
        assert!(rec.acted);
        assert_eq!(*fakes.kills.lock().unwrap(), vec![51]);
    }

    #[tokio::test(start_paused = true)]
    async fn already_closed_during_grace_is_not_killed() {
        let fakes = Arc::new(Fakes::default());
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rules(ENFORCE)));
        let rec = e.handle_process(&game(52)).await.unwrap();
        assert!(rec.acted);
        assert!(fakes.close_requests.lock().unwrap().is_empty());
        assert!(fakes.kills.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn dry_run_records_but_does_not_act() {
        let fakes = Arc::new(Fakes::default());
        fakes.running.lock().unwrap().insert(53);
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rules(r#"{"revision":1,"dry_run":true}"#)));
        let rec = e.handle_process(&game(53)).await.unwrap();
        assert!(rec.dry_run && !rec.acted);
        assert_eq!(rec.kind, ViolationKind::AppBlocked);
        assert!(rec.screenshot_jpeg.is_some(), "evidence attached");
        assert!(fakes.warnings.lock().unwrap().is_empty());
    }

    #[tokio::test(start_paused = true)]
    async fn free_mode_only_watches() {
        let fakes = Arc::new(Fakes::default());
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rules(r#"{"revision":1,"mode":"free","dry_run":false}"#)));
        let rec = e.handle_process(&game(54)).await.unwrap();
        assert!(rec.dry_run && !rec.acted);
    }

    #[tokio::test(start_paused = true)]
    async fn paused_or_without_rules_does_nothing() {
        let fakes = Arc::new(Fakes::default());
        let (e, _rx) = engine(&fakes, AgentState::Paused, Some(rules(ENFORCE)));
        assert!(e.handle_process(&game(55)).await.is_none());
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, None);
        assert!(e.handle_process(&game(55)).await.is_none());
        assert_eq!(*fakes.captures.lock().unwrap(), 0, "nothing captured");
    }

    #[tokio::test(start_paused = true)]
    async fn whitelisted_and_system_processes_are_left_alone() {
        let fakes = Arc::new(Fakes::default());
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rules(ENFORCE)));
        let mut chrome = game(56);
        chrome.name = "chrome.exe".into();
        assert!(e.handle_process(&chrome).await.is_none());
        let mut svc = game(57);
        svc.name = "svchost.exe".into();
        assert!(e.handle_process(&svc).await.is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn the_same_pid_is_handled_once() {
        let fakes = Arc::new(Fakes::default());
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rules(r#"{"revision":1}"#)));
        assert!(e.handle_process(&game(58)).await.is_some());
        assert!(e.handle_process(&game(58)).await.is_none());
    }

    #[tokio::test(start_paused = true)]
    async fn exam_closes_a_coursework_app() {
        let fakes = Arc::new(Fakes::default());
        let rs = rules(
            r#"{"revision":1,"mode":"exam","dry_run":false,"close_grace_seconds":0,
            "app_whitelist":[{"process_name":"code.exe"}]}"#,
        );
        let (e, _rx) = engine(&fakes, AgentState::Enforcing, Some(rs));
        let mut code = game(59);
        code.name = "code.exe".into();
        assert!(e.handle_process(&code).await.unwrap().acted);
    }
}
