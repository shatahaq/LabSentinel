//! New-process detection by diffing snapshots on an interval.
//!
//! A process is reported when it first appears, and again the moment its first
//! window appears: the safety gate only acts on windowed processes, and many
//! launchers and games show their window seconds after starting.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use super::identity::ProcessInfo;
use super::BoxFut;

pub type Snapshot = Arc<dyn Fn() -> BoxFut<'static, Vec<ProcessInfo>> + Send + Sync>;

/// Tracks what was seen; `next` returns what to report from a new snapshot.
#[derive(Default)]
pub struct Diff {
    seen: HashSet<u32>,
    windowed: HashSet<u32>,
}

impl Diff {
    pub fn prime(&mut self, procs: &[ProcessInfo]) {
        self.seen = procs.iter().map(|p| p.pid).collect();
        self.windowed = procs.iter().filter(|p| p.has_visible_window).map(|p| p.pid).collect();
    }

    pub fn next(&mut self, procs: Vec<ProcessInfo>) -> Vec<ProcessInfo> {
        let report: Vec<ProcessInfo> = procs
            .iter()
            .filter(|p| !self.seen.contains(&p.pid) || (p.has_visible_window && !self.windowed.contains(&p.pid)))
            .cloned()
            .collect();
        self.prime(&procs);
        report
    }
}

/// Poll forever, calling `on_new` for every new (or newly windowed) process.
pub async fn watch<F>(snapshot: Snapshot, interval: Duration, mut on_new: F)
where
    F: FnMut(ProcessInfo) + Send,
{
    let mut diff = Diff::default();
    diff.prime(&snapshot().await);
    loop {
        tokio::time::sleep(interval).await;
        for proc_ in diff.next(snapshot().await) {
            on_new(proc_);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(pid: u32, window: bool) -> ProcessInfo {
        ProcessInfo { pid, name: format!("p{pid}.exe"), has_visible_window: window, ..Default::default() }
    }

    #[test]
    fn reports_new_processes_once() {
        let mut d = Diff::default();
        d.prime(&[p(1, true)]);
        let new = d.next(vec![p(1, true), p(2, true)]);
        assert_eq!(new.iter().map(|x| x.pid).collect::<Vec<_>>(), vec![2]);
        assert!(d.next(vec![p(1, true), p(2, true)]).is_empty());
    }

    #[test]
    fn reports_again_when_the_window_appears() {
        let mut d = Diff::default();
        d.prime(&[]);
        assert_eq!(d.next(vec![p(3, false)]).len(), 1, "new, still windowless");
        assert_eq!(d.next(vec![p(3, true)]).len(), 1, "its window appeared");
        assert!(d.next(vec![p(3, true)]).is_empty());
    }

    #[test]
    fn a_reused_pid_after_exit_counts_as_new() {
        let mut d = Diff::default();
        d.prime(&[p(4, true)]);
        assert!(d.next(vec![]).is_empty());
        assert_eq!(d.next(vec![p(4, true)]).len(), 1);
    }
}
