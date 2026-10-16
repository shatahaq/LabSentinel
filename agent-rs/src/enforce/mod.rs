//! Enforcement: pure decision logic (safety gate, whitelist matching, the
//! warn → grace → close → kill flow) behind small ports, so it is tested with
//! fakes on any OS and driven by the Windows layer in production.

pub mod detector;
pub mod engine;
pub mod identity;
pub mod matcher;
pub mod safety;
pub mod title_watch;

use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;

pub type BoxFut<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The participant's desktop, reached directly (console) or through the
/// desktop worker in their session (service mode).
pub trait Desktop: Send + Sync {
    /// pid -> one visible window title. `None` = cannot see the desktop now.
    fn window_index(&self) -> BoxFut<'_, Option<HashMap<u32, String>>>;
    /// (process name, title) of the foreground window.
    fn foreground(&self) -> BoxFut<'_, (String, String)>;
    fn capture_jpeg(&self, max_width: u32, quality: u8) -> BoxFut<'_, Option<Vec<u8>>>;
    /// Ask every visible window of `pid` to close (WM_CLOSE).
    fn close_windows(&self, pid: u32) -> BoxFut<'_, bool>;
    fn lock(&self) -> BoxFut<'_, bool>;
    fn freeze(&self, on: bool) -> BoxFut<'_, bool>;
    /// Whether the desktop can be seen right now (reported in heartbeats).
    fn available(&self) -> bool;
}

/// Ends processes. Only ever called for evaluable violations.
pub trait ProcessControl: Send + Sync {
    fn is_running(&self, pid: u32) -> bool;
    /// Last-resort hard kill. True if the process is gone.
    fn kill(&self, pid: u32) -> bool;
}

/// Tells the participant what is happening, without stealing focus.
pub trait Warner: Send + Sync {
    fn warn_closing(&self, app: &str, seconds: u32, reason: &str);
    fn notify(&self, title: &str, message: &str);
}
