//! Adaptive screen streaming: OFF (nobody watching), GRID (small, ~5 fps) and
//! HIRES (an admin has this PC open, ~10 fps).
//!
//! Nothing is captured unless someone is watching and monitoring is not
//! paused. An unchanged screen is only re-sent as a keep-alive every few
//! seconds, and frames are dropped (not queued) when the link is busy.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::time::{sleep, Instant};

use crate::enforce::Desktop;
use crate::protocol::{AgentState, StreamMode};

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Profile {
    pub fps: f64,
    pub max_width: u32,
    pub quality: u8,
}

pub fn profile(mode: StreamMode) -> Option<Profile> {
    match mode {
        StreamMode::Off => None,
        StreamMode::Grid => Some(Profile { fps: 5.0, max_width: 400, quality: 45 }),
        StreamMode::Hires => Some(Profile { fps: 10.0, max_width: 1280, quality: 65 }),
    }
}

const IDLE_POLL: Duration = Duration::from_millis(250);
pub const UNCHANGED_RESEND: Duration = Duration::from_secs(3);

/// (jpeg, width, height, hi_res) -> delivered?
pub type FrameSender = Arc<dyn Fn(Vec<u8>, u32, u32, bool) -> bool + Send + Sync>;

#[derive(Default)]
struct Last {
    frame: Option<Vec<u8>>,
    sent_at: Option<Instant>,
}

pub struct Streamer {
    mode: Mutex<StreamMode>,
    last: Mutex<Last>,
    pub frames_sent: Mutex<u64>,
}

impl Default for Streamer {
    fn default() -> Self {
        Self { mode: Mutex::new(StreamMode::Off), last: Mutex::new(Last::default()), frames_sent: Mutex::new(0) }
    }
}

impl Streamer {
    pub fn mode(&self) -> StreamMode {
        *self.mode.lock().unwrap()
    }

    pub fn set_mode(&self, mode: StreamMode) {
        let mut current = self.mode.lock().unwrap();
        if *current != mode {
            log::info!("stream mode: {:?} -> {:?}", *current, mode);
            *current = mode;
            *self.last.lock().unwrap() = Last::default(); // a new view gets a fresh frame
        }
    }

    fn should_capture(&self, state: AgentState) -> bool {
        self.mode() != StreamMode::Off && state != AgentState::Paused
    }

    pub async fn run(
        self: Arc<Self>,
        desktop: Arc<dyn Desktop>,
        state: Arc<dyn Fn() -> AgentState + Send + Sync>,
        send: FrameSender,
    ) {
        loop {
            let mode = self.mode();
            let Some(p) = profile(mode).filter(|_| self.should_capture(state())) else {
                sleep(IDLE_POLL).await;
                continue;
            };
            let started = Instant::now();
            self.tick(&*desktop, &*state, &send, p, mode == StreamMode::Hires).await;
            let period = Duration::from_secs_f64(1.0 / p.fps);
            sleep(period.saturating_sub(started.elapsed())).await;
        }
    }

    async fn tick(&self, desktop: &dyn Desktop, state: &(dyn Fn() -> AgentState + Send + Sync), send: &FrameSender, p: Profile, hi_res: bool) {
        let Some(jpeg) = desktop.capture_jpeg(p.max_width, p.quality).await else { return };
        // The admin may have stopped watching while we captured.
        if !self.should_capture(state()) {
            return;
        }
        let now = Instant::now();
        {
            let last = self.last.lock().unwrap();
            if last.frame.as_deref() == Some(&jpeg[..])
                && last.sent_at.is_some_and(|t| now.duration_since(t) < UNCHANGED_RESEND)
            {
                return; // same screen: skip it, the keep-alive comes later
            }
        }
        let (w, h) = jpeg_size(&jpeg).unwrap_or((p.max_width, (p.max_width * 9 / 16).max(1)));
        if send(jpeg.clone(), w, h, hi_res) {
            *self.frames_sent.lock().unwrap() += 1;
            *self.last.lock().unwrap() = Last { frame: Some(jpeg), sent_at: Some(now) };
        }
    }
}

/// Width/height from the JPEG SOF marker, without decoding the image.
pub fn jpeg_size(jpeg: &[u8]) -> Option<(u32, u32)> {
    let n = jpeg.len();
    let mut i = 2;
    while i + 9 < n {
        if jpeg[i] != 0xFF {
            i += 1;
            continue;
        }
        let marker = jpeg[i + 1];
        if (0xC0..=0xCF).contains(&marker) && ![0xC4, 0xC8, 0xCC].contains(&marker) {
            let h = u16::from_be_bytes([jpeg[i + 5], jpeg[i + 6]]) as u32;
            let w = u16::from_be_bytes([jpeg[i + 7], jpeg[i + 8]]) as u32;
            return (w > 0 && h > 0).then_some((w, h));
        }
        if marker == 0xD8 || marker == 0x01 || (0xD0..=0xD7).contains(&marker) {
            i += 2;
            continue;
        }
        let segment = u16::from_be_bytes([jpeg[i + 2], jpeg[i + 3]]) as usize;
        if segment == 0 {
            return None;
        }
        i += 2 + segment;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::enforce::BoxFut;
    use std::collections::HashMap;

    /// A desktop whose screen never changes; counts captures.
    struct StillScreen {
        captures: Mutex<u32>,
    }

    impl Desktop for StillScreen {
        fn window_index(&self) -> BoxFut<'_, Option<HashMap<u32, String>>> {
            Box::pin(async { None })
        }
        fn foreground(&self) -> BoxFut<'_, (String, String)> {
            Box::pin(async { (String::new(), String::new()) })
        }
        fn capture_jpeg(&self, w: u32, _q: u8) -> BoxFut<'_, Option<Vec<u8>>> {
            *self.captures.lock().unwrap() += 1;
            Box::pin(async move { Some(crate::jpeg::encode_rgb(&vec![90u8; (w * 10 * 3) as usize], w, 10, 50).unwrap()) })
        }
        fn close_windows(&self, _p: u32) -> BoxFut<'_, bool> {
            Box::pin(async { false })
        }
        fn lock(&self) -> BoxFut<'_, bool> {
            Box::pin(async { false })
        }
        fn freeze(&self, _o: bool) -> BoxFut<'_, bool> {
            Box::pin(async { false })
        }
        fn available(&self) -> bool {
            true
        }
    }

    #[test]
    fn reads_the_size_of_an_encoded_frame() {
        let jpeg = crate::jpeg::encode_rgb(&vec![0u8; 64 * 36 * 3], 64, 36, 60).unwrap();
        assert_eq!(jpeg_size(&jpeg), Some((64, 36)));
        assert_eq!(jpeg_size(b"nope"), None);
    }

    #[tokio::test(start_paused = true)]
    async fn captures_nothing_when_off_or_paused() {
        let desktop = Arc::new(StillScreen { captures: Mutex::new(0) });
        let streamer = Arc::new(Streamer::default());
        let sent = Arc::new(Mutex::new(0u32));
        let s = sent.clone();
        let send: FrameSender = Arc::new(move |_, _, _, _| {
            *s.lock().unwrap() += 1;
            true
        });
        let paused = Arc::new(Mutex::new(false));
        let p = paused.clone();
        let state = Arc::new(move || if *p.lock().unwrap() { AgentState::Paused } else { AgentState::Enforcing });
        tokio::spawn(streamer.clone().run(desktop.clone(), state, send));

        tokio::time::sleep(Duration::from_secs(2)).await;
        assert_eq!(*desktop.captures.lock().unwrap(), 0, "OFF: nothing captured");

        streamer.set_mode(StreamMode::Grid);
        tokio::time::sleep(Duration::from_secs(2)).await;
        let grabbed = *desktop.captures.lock().unwrap();
        assert!(grabbed >= 8, "about 5 fps, got {grabbed}");
        assert_eq!(*sent.lock().unwrap(), 1, "a still screen is sent once, then skipped");

        tokio::time::sleep(UNCHANGED_RESEND).await;
        assert_eq!(*sent.lock().unwrap(), 2, "keep-alive after a few seconds");

        *paused.lock().unwrap() = true;
        tokio::time::sleep(Duration::from_millis(300)).await;
        let before = *desktop.captures.lock().unwrap();
        tokio::time::sleep(Duration::from_secs(2)).await;
        assert_eq!(*desktop.captures.lock().unwrap(), before, "paused: nothing captured");
    }
}
