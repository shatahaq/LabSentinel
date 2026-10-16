//! WebSocket link to the dashboard with automatic reconnection, mirroring
//! `agent/labsentinel_agent/transport/client.py`.
//!
//! One session: connect, HELLO (with the pairing code on first contact), wait
//! for WELCOME (adopting a freshly issued token), then run a heartbeat loop and
//! a receive loop that hands rule updates and commands to the agent and sends
//! back an ACK for every command. On any failure it reconnects with
//! exponential backoff plus jitter; enforcement elsewhere keeps running on the
//! cached rules meanwhile.

use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::{mpsc, oneshot, watch};
use tokio::time::timeout;
use tokio_tungstenite::tungstenite::protocol::WebSocketConfig;
use tokio_tungstenite::tungstenite::Message;

use crate::protocol::{
    self, Ack, AgentState, Command, Goodbye, Heartbeat, Hello, Payload, MAX_MESSAGE_BYTES,
};
use crate::rules::RuleSet;

const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(10);
const OUTBOX: usize = 256;

#[derive(Clone, Debug)]
pub struct Identity {
    pub agent_id: String,
    pub hostname: String,
    pub username: String,
    pub agent_version: String,
    pub os_version: String,
}

/// What a command handler reports; the client turns it into an ACK.
#[derive(Clone, Debug)]
pub struct CommandResult {
    pub resulting_state: AgentState,
    pub ok: bool,
    pub message: String,
}

/// Everything the heartbeat reports, gathered by the agent at send time.
#[derive(Clone, Debug, Default)]
pub struct HeartbeatInfo {
    pub state: AgentState,
    pub rule_revision: i64,
    pub cpu_percent: f64,
    pub mem_mb: f64,
    pub active_window_title: String,
    pub indicator_visible: bool,
    pub desktop_access: bool,
    pub input_frozen: bool,
    pub username: Option<String>,
}

/// Messages from the dashboard that the agent must act on.
pub enum Inbound {
    Ruleset(RuleSet, oneshot::Sender<()>),
    Command(Command, oneshot::Sender<CommandResult>),
}

#[derive(Clone, Copy, Debug)]
pub struct Backoff {
    pub base_s: f64,
    pub factor: f64,
    pub max_s: f64,
    pub jitter_s: f64,
}

impl Default for Backoff {
    fn default() -> Self {
        Self { base_s: 1.0, factor: 2.0, max_s: 30.0, jitter_s: 0.5 }
    }
}

impl Backoff {
    pub fn delay(&self, attempt: u32) -> Duration {
        let exp = self.factor.powi(attempt.saturating_sub(1).min(30) as i32);
        let raw = (self.base_s * exp).min(self.max_s);
        let jitter = (getrandom::u64().unwrap_or(0) as f64 / u64::MAX as f64) * self.jitter_s;
        Duration::from_secs_f64(raw + jitter)
    }
}

/// The send side of the link, shared with everything that reports events.
#[derive(Clone)]
pub struct Link {
    inner: Arc<LinkInner>,
}

struct LinkInner {
    agent_id: String,
    token: Mutex<Option<String>>,
    outbox: Mutex<Option<mpsc::Sender<String>>>,
    connected: watch::Sender<bool>,
}

impl Link {
    fn new(agent_id: String, token: Option<String>) -> Self {
        let (connected, _) = watch::channel(false);
        Self {
            inner: Arc::new(LinkInner {
                agent_id,
                token: Mutex::new(token),
                outbox: Mutex::new(None),
                connected,
            }),
        }
    }

    pub fn agent_id(&self) -> &str {
        &self.inner.agent_id
    }

    pub fn is_connected(&self) -> bool {
        *self.inner.connected.borrow()
    }

    /// Resolves once the link is up (immediately if it already is).
    pub async fn wait_connected(&self) {
        let mut rx = self.inner.connected.subscribe();
        let _ = rx.wait_for(|up| *up).await;
    }

    fn token(&self) -> Option<String> {
        self.inner.token.lock().unwrap().clone()
    }

    fn set_token(&self, token: String) {
        *self.inner.token.lock().unwrap() = Some(token);
    }

    fn attach(&self, tx: mpsc::Sender<String>) {
        *self.inner.outbox.lock().unwrap() = Some(tx);
        self.inner.connected.send_replace(true);
    }

    fn detach(&self) {
        *self.inner.outbox.lock().unwrap() = None;
        self.inner.connected.send_replace(false);
    }

    fn outbox(&self) -> Option<mpsc::Sender<String>> {
        if !self.is_connected() {
            return None;
        }
        self.inner.outbox.lock().unwrap().clone()
    }

    /// Send an agent event (violation, ACK, heartbeat). False if offline.
    pub async fn send(&self, payload: Payload) -> bool {
        let Some(tx) = self.outbox() else { return false };
        tx.send(protocol::encode(payload, self.token().as_deref())).await.is_ok()
    }

    /// Send without waiting: a screen frame is dropped rather than queued when
    /// the link is busy, so a slow network delays nothing else.
    pub fn try_send(&self, payload: Payload) -> bool {
        let Some(tx) = self.outbox() else { return false };
        tx.try_send(protocol::encode(payload, self.token().as_deref())).is_ok()
    }
}

pub type UrlFuture = Pin<Box<dyn Future<Output = Option<String>> + Send>>;
pub type UrlProvider = Arc<dyn Fn() -> UrlFuture + Send + Sync>;
pub type HeartbeatSource = Arc<dyn Fn() -> HeartbeatInfo + Send + Sync>;
pub type TokenSink = Arc<dyn Fn(&str) + Send + Sync>;

pub struct ClientOptions {
    pub identity: Identity,
    pub token: Option<String>,
    pub pairing_code: Option<String>,
    pub on_token_issued: Option<TokenSink>,
    pub heartbeat: HeartbeatSource,
    pub cached_revision: Arc<dyn Fn() -> i64 + Send + Sync>,
    pub heartbeat_interval_s: f64,
    pub backoff: Backoff,
}

pub struct DashboardClient {
    opts: ClientOptions,
    pairing_code: Mutex<Option<String>>,
    link: Link,
    inbound: mpsc::Sender<Inbound>,
    stop: watch::Sender<bool>,
    /// Set when the dashboard refused our pairing code; `run` then returns.
    pairing_rejected: Mutex<Option<String>>,
}

impl DashboardClient {
    pub fn new(opts: ClientOptions, inbound: mpsc::Sender<Inbound>) -> Arc<Self> {
        let link = Link::new(opts.identity.agent_id.clone(), opts.token.clone());
        let (stop, _) = watch::channel(false);
        Arc::new(Self {
            pairing_code: Mutex::new(opts.pairing_code.clone()),
            opts,
            link,
            inbound,
            stop,
            pairing_rejected: Mutex::new(None),
        })
    }

    pub fn link(&self) -> Link {
        self.link.clone()
    }

    pub fn pairing_rejected(&self) -> Option<String> {
        self.pairing_rejected.lock().unwrap().clone()
    }

    fn stopped(&self) -> bool {
        *self.stop.borrow()
    }

    /// Ask the link to end: a GOODBYE is sent and `run` returns.
    pub fn stop(&self) {
        self.stop.send_replace(true);
    }

    async fn sleep_or_stop(&self, d: Duration) {
        let mut rx = self.stop.subscribe();
        let _ = timeout(d, rx.wait_for(|s| *s)).await;
    }

    /// Connect and reconnect until stopped (or the pairing code is refused).
    pub async fn run(self: Arc<Self>, url_provider: UrlProvider) {
        let mut attempt: u32 = 0;
        while !self.stopped() {
            let Some(url) = url_provider().await else {
                attempt += 1;
                self.sleep_or_stop(self.opts.backoff.delay(attempt)).await;
                continue;
            };
            match self.session(&url).await {
                Ok(()) => attempt = 0,
                Err(e) => log::info!("connection to {url} failed/closed: {e}"),
            }
            self.link.detach();
            if self.stopped() {
                break;
            }
            attempt += 1;
            let delay = self.opts.backoff.delay(attempt);
            log::info!("reconnecting in {:.1}s (attempt {attempt})", delay.as_secs_f64());
            self.sleep_or_stop(delay).await;
        }
    }

    async fn session(&self, url: &str) -> Result<(), String> {
        let config = WebSocketConfig::default()
            .max_message_size(Some(MAX_MESSAGE_BYTES))
            .max_frame_size(Some(MAX_MESSAGE_BYTES));
        // tungstenite connects straight to the address: no system proxy is
        // involved, which matters on school networks (the dashboard is on the LAN).
        let (ws, _) = timeout(
            HANDSHAKE_TIMEOUT,
            tokio_tungstenite::connect_async_with_config(url, Some(config), true),
        )
        .await
        .map_err(|_| "connect timed out".to_string())?
        .map_err(|e| e.to_string())?;
        let (mut sink, mut stream) = ws.split();

        let token = self.link.token();
        let pairing_code = if token.is_some() { None } else { self.pairing_code.lock().unwrap().clone() };
        let id = &self.opts.identity;
        let hello = Payload::Hello(Hello {
            agent_id: id.agent_id.clone(),
            hostname: id.hostname.clone(),
            username: id.username.clone(),
            agent_version: id.agent_version.clone(),
            os_version: id.os_version.clone(),
            cached_rule_revision: (self.opts.cached_revision)(),
            pairing_code,
        });
        sink.send(Message::text(protocol::encode(hello, token.as_deref())))
            .await
            .map_err(|e| e.to_string())?;

        // WELCOME or REJECT.
        let first = timeout(HANDSHAKE_TIMEOUT, next_text(&mut stream))
            .await
            .map_err(|_| "no welcome from the dashboard".to_string())?
            .ok_or("closed before welcome")?;
        let welcome = match protocol::decode(&first).map_err(|e| e.to_string())?.payload {
            Payload::Welcome(w) => w,
            Payload::Reject(r) => {
                log::error!("dashboard rejected agent: {}", r.reason);
                if token.is_none() && self.pairing_code.lock().unwrap().is_some() {
                    // A refused code does not become right by retrying, and every
                    // retry counts as a guess toward the dashboard's lockout.
                    *self.pairing_rejected.lock().unwrap() = Some(r.reason.clone());
                    self.stop();
                }
                return Err(format!("rejected: {}", r.reason));
            }
            _ => return Ok(()),
        };
        if let Some(issued) = &welcome.agent_token {
            self.link.set_token(issued.clone());
            *self.pairing_code.lock().unwrap() = None;
            if let Some(sink) = &self.opts.on_token_issued {
                sink(issued);
            }
            log::info!("paired with the dashboard; credential stored");
        }

        let (tx, mut rx) = mpsc::channel::<String>(OUTBOX);
        self.link.attach(tx);
        log::info!("joined session {}", welcome.session_id);

        // Single writer: everything leaves through one task in order.
        let writer = tokio::spawn(async move {
            while let Some(text) = rx.recv().await {
                if sink.send(Message::text(text)).await.is_err() {
                    break;
                }
            }
            let _ = sink.close().await;
        });

        self.deliver_ruleset(welcome.ruleset).await;

        let hb_link = self.link.clone();
        let hb_source = self.opts.heartbeat.clone();
        let hb_every = Duration::from_secs_f64(welcome.heartbeat_interval_s);
        let agent_id = self.opts.identity.agent_id.clone();
        let heartbeat = tokio::spawn(async move {
            loop {
                let info = hb_source();
                let sent = hb_link
                    .send(Payload::Heartbeat(Heartbeat {
                        agent_id: agent_id.clone(),
                        state: info.state,
                        rule_revision: info.rule_revision,
                        cpu_percent: info.cpu_percent,
                        mem_mb: info.mem_mb,
                        active_window_title: info.active_window_title,
                        indicator_visible: info.indicator_visible,
                        desktop_access: info.desktop_access,
                        input_frozen: info.input_frozen,
                        username: info.username,
                    }))
                    .await;
                if !sent {
                    break;
                }
                tokio::time::sleep(hb_every).await;
            }
        });

        let mut stop_rx = self.stop.subscribe();
        let result = loop {
            tokio::select! {
                _ = stop_rx.wait_for(|s| *s) => {
                    self.link
                        .send(Payload::Goodbye(Goodbye {
                            agent_id: self.opts.identity.agent_id.clone(),
                            reason: "shutdown".into(),
                        }))
                        .await;
                    break Ok(());
                }
                msg = next_text(&mut stream) => {
                    let Some(text) = msg else { break Ok(()) };
                    self.handle(&text).await;
                }
            }
        };

        heartbeat.abort();
        self.link.detach(); // drops the outbox: the writer flushes and closes
        let _ = timeout(Duration::from_secs(3), writer).await;
        result
    }

    async fn deliver_ruleset(&self, ruleset: RuleSet) {
        let (done, wait) = oneshot::channel();
        if self.inbound.send(Inbound::Ruleset(ruleset, done)).await.is_ok() {
            let _ = wait.await;
        }
    }

    async fn handle(&self, text: &str) {
        let env = match protocol::decode(text) {
            Ok(env) => env,
            Err(e) => {
                log::warn!("dropping bad message: {e}");
                return;
            }
        };
        match env.payload {
            Payload::RulesUpdate(r) => self.deliver_ruleset(r.ruleset).await,
            Payload::Command(cmd) => {
                let (reply, wait) = oneshot::channel();
                if self.inbound.send(Inbound::Command(cmd, reply)).await.is_err() {
                    return;
                }
                let Ok(result) = wait.await else { return };
                self.link
                    .send(Payload::Ack(Ack {
                        agent_id: self.opts.identity.agent_id.clone(),
                        ack_id: env.id,
                        ok: result.ok,
                        resulting_state: result.resulting_state,
                        message: result.message,
                    }))
                    .await;
            }
            other => log::debug!("ignoring {:?}", std::mem::discriminant(&other)),
        }
    }
}

/// Next text message; `None` when the connection is closed.
async fn next_text<S>(stream: &mut S) -> Option<String>
where
    S: futures_util::Stream<Item = Result<Message, tokio_tungstenite::tungstenite::Error>> + Unpin,
{
    while let Some(item) = stream.next().await {
        match item {
            Ok(Message::Text(t)) => return Some(t.to_string()),
            Ok(Message::Binary(b)) => return String::from_utf8(b.to_vec()).ok(),
            Ok(Message::Close(_)) | Err(_) => return None,
            Ok(_) => continue, // ping/pong are answered by tungstenite
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_grows_and_caps() {
        let b = Backoff { base_s: 1.0, factor: 2.0, max_s: 30.0, jitter_s: 0.0 };
        assert_eq!(b.delay(1), Duration::from_secs(1));
        assert_eq!(b.delay(3), Duration::from_secs(4));
        assert_eq!(b.delay(20), Duration::from_secs(30));
    }

    #[tokio::test]
    async fn link_refuses_to_send_while_offline() {
        let link = Link::new("pc".into(), None);
        assert!(!link.is_connected());
        assert!(!link.try_send(Payload::Goodbye(Goodbye { agent_id: "pc".into(), reason: "x".into() })));
        let (tx, mut rx) = mpsc::channel(4);
        link.attach(tx);
        assert!(link.send(Payload::Goodbye(Goodbye { agent_id: "pc".into(), reason: "x".into() })).await);
        let sent = rx.recv().await.unwrap();
        assert!(sent.contains("\"goodbye\""));
        link.detach();
        assert!(!link.is_connected());
    }
}
