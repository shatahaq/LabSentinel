//! Wire protocol between agent and dashboard, identical to
//! `shared/labsentinel_shared/protocol.py`.
//!
//! Every message is a JSON envelope `{v, id, ts, token, payload}` whose payload
//! carries a `type` discriminator. Outgoing text is cut to the protocol's
//! limits before sending, because the dashboard rejects an over-long field and
//! with it the whole message.

use serde::{Deserialize, Serialize};

use crate::rules::{EnforcementMode, RuleSet};
use crate::util::{now_ts, random_hex, truncate_chars};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_MESSAGE_BYTES: usize = 32 * 1024 * 1024;

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum AgentState {
    #[default]
    Enforcing,
    Paused,
    DryRun,
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ViolationKind {
    AppBlocked,
    AppClosed,
    Impersonation,
    SiteTitle,
    AgentTamper,
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum CommandType {
    Pause,
    Resume,
    SetDryRun,
    LockScreen,
    FreezeInput,
    BroadcastMessage,
    SetStream,
}

#[derive(Serialize, Deserialize, Clone, Copy, Debug, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum StreamMode {
    #[default]
    Off,
    Grid,
    Hires,
}

impl StreamMode {
    pub fn parse(text: &str) -> Option<Self> {
        match text.trim().to_lowercase().as_str() {
            "off" => Some(Self::Off),
            "grid" => Some(Self::Grid),
            "hires" => Some(Self::Hires),
            _ => None,
        }
    }
}

// ----------------------------------------------------------- agent -> dashboard

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Hello {
    pub agent_id: String,
    pub hostname: String,
    pub username: String,
    pub agent_version: String,
    pub os_version: String,
    pub cached_rule_revision: i64,
    pub pairing_code: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Heartbeat {
    pub agent_id: String,
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

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Violation {
    pub agent_id: String,
    pub kind: ViolationKind,
    pub subject: String,
    pub detail: String,
    pub detected_from_title: bool,
    pub dry_run: bool,
    pub occurred_at: f64,
    pub screenshot_b64: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct ScreenFrame {
    pub agent_id: String,
    pub hi_res: bool,
    pub width: u32,
    pub height: u32,
    pub jpeg_b64: String,
    pub captured_at: f64,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Ack {
    pub agent_id: String,
    pub ack_id: String,
    pub ok: bool,
    pub resulting_state: AgentState,
    pub message: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Goodbye {
    pub agent_id: String,
    pub reason: String,
}

// ----------------------------------------------------------- dashboard -> agent

fn default_hb() -> f64 {
    5.0
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Welcome {
    pub session_id: String,
    pub server_time: f64,
    #[serde(default = "default_hb")]
    pub heartbeat_interval_s: f64,
    pub ruleset: RuleSet,
    #[serde(default)]
    pub agent_token: Option<String>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Reject {
    pub reason: String,
    #[serde(default)]
    pub server_protocol_version: Option<u32>,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct RulesUpdate {
    pub ruleset: RuleSet,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Command {
    pub command: CommandType,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub mode: Option<EnforcementMode>,
    #[serde(default)]
    pub issued_by: String,
}

#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Payload {
    Hello(Hello),
    Heartbeat(Heartbeat),
    Violation(Violation),
    ScreenFrame(ScreenFrame),
    Ack(Ack),
    Goodbye(Goodbye),
    Welcome(Welcome),
    Reject(Reject),
    RulesUpdate(RulesUpdate),
    Command(Command),
}

impl Payload {
    /// Bring every text field within the dashboard's limits.
    fn clamp(&mut self) {
        let t = truncate_chars;
        match self {
            Payload::Hello(h) => {
                h.agent_id = t(&h.agent_id, 64);
                h.hostname = t(&h.hostname, 128);
                h.username = t(&h.username, 128);
                h.agent_version = t(&h.agent_version, 32);
                h.os_version = t(&h.os_version, 64);
                h.cached_rule_revision = h.cached_rule_revision.max(-1);
                h.pairing_code = h.pairing_code.as_deref().map(|c| t(c, 32));
            }
            Payload::Heartbeat(hb) => {
                hb.cpu_percent = hb.cpu_percent.clamp(0.0, 100.0);
                hb.mem_mb = hb.mem_mb.max(0.0);
                hb.active_window_title = t(&hb.active_window_title, 256);
                hb.username = hb.username.as_deref().map(|u| t(u, 256));
                hb.rule_revision = hb.rule_revision.max(-1);
            }
            Payload::Violation(v) => {
                v.subject = t(&v.subject, 512);
                v.detail = t(&v.detail, 1024);
            }
            Payload::Ack(a) => a.message = t(&a.message, 256),
            Payload::Goodbye(g) => g.reason = t(&g.reason, 128),
            _ => {}
        }
    }
}

/// The only object that ever touches the socket.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Envelope {
    pub v: u32,
    pub id: String,
    pub ts: f64,
    #[serde(default)]
    pub token: Option<String>,
    pub payload: Payload,
}

/// Wrap a payload in an envelope and serialise it.
pub fn encode(mut payload: Payload, token: Option<&str>) -> String {
    payload.clamp();
    let env = Envelope {
        v: PROTOCOL_VERSION,
        id: random_hex(16),
        ts: now_ts(),
        token: token.map(str::to_string),
        payload,
    };
    serde_json::to_string(&env).expect("envelope always serialises")
}

#[derive(Debug)]
pub struct ProtocolError(pub String);

impl std::fmt::Display for ProtocolError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ProtocolError {}

/// Parse and validate an incoming message. Anything malformed is an error the
/// caller drops; it is never acted on.
pub fn decode(raw: &str) -> Result<Envelope, ProtocolError> {
    if raw.len() > MAX_MESSAGE_BYTES {
        return Err(ProtocolError(format!("message too large: {} bytes", raw.len())));
    }
    let mut env: Envelope =
        serde_json::from_str(raw).map_err(|e| ProtocolError(format!("invalid message: {e}")))?;
    match &mut env.payload {
        Payload::Welcome(w) => {
            if !(w.heartbeat_interval_s > 0.0 && w.heartbeat_interval_s <= 60.0) {
                return Err(ProtocolError("heartbeat interval out of range".into()));
            }
            w.ruleset = w.ruleset.clone().validated().map_err(ProtocolError)?;
        }
        Payload::RulesUpdate(r) => {
            r.ruleset = r.ruleset.clone().validated().map_err(ProtocolError)?;
        }
        Payload::Command(c) => {
            if c.text.chars().count() > 1024 {
                return Err(ProtocolError("command text too long".into()));
            }
        }
        _ => {}
    }
    Ok(env)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn heartbeat_serialises_like_the_python_model() {
        let raw = encode(
            Payload::Heartbeat(Heartbeat {
                agent_id: "pc-1".into(),
                state: AgentState::DryRun,
                rule_revision: 4,
                cpu_percent: 250.0,
                mem_mb: 12.5,
                active_window_title: "x".repeat(300),
                indicator_visible: true,
                desktop_access: true,
                input_frozen: false,
                username: Some("andi".into()),
            }),
            Some("tok"),
        );
        let value: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(value["v"], 1);
        assert_eq!(value["token"], "tok");
        assert_eq!(value["id"].as_str().unwrap().len(), 32);
        let p = &value["payload"];
        assert_eq!(p["type"], "heartbeat");
        assert_eq!(p["state"], "dry_run");
        assert_eq!(p["cpu_percent"], 100.0, "clamped to the field's range");
        assert_eq!(p["active_window_title"].as_str().unwrap().chars().count(), 256);
    }

    #[test]
    fn decodes_a_welcome_from_the_dashboard() {
        let raw = r#"{"v":1,"id":"abc","ts":1700000000.5,"token":null,
          "payload":{"type":"welcome","session_id":"s1","server_time":1.0,
          "heartbeat_interval_s":5.0,"agent_token":"T",
          "ruleset":{"revision":2,"mode":"lab","dry_run":true,"close_grace_seconds":5,
          "app_whitelist":[],"site_blocklist":[],"block_incognito":true}}}"#;
        let env = decode(raw).unwrap();
        match env.payload {
            Payload::Welcome(w) => {
                assert_eq!(w.ruleset.revision, 2);
                assert_eq!(w.agent_token.as_deref(), Some("T"));
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn decodes_commands_and_rejects_unknown_ones() {
        let ok = r#"{"v":1,"id":"a","ts":1.0,"payload":{"type":"command",
            "command":"freeze_input","text":"on","mode":null,"issued_by":"pengawas"}}"#;
        match decode(ok).unwrap().payload {
            Payload::Command(c) => {
                assert_eq!(c.command, CommandType::FreezeInput);
                assert_eq!(c.text, "on");
            }
            other => panic!("unexpected {other:?}"),
        }
        let bad = r#"{"v":1,"id":"a","ts":1.0,"payload":{"type":"command","command":"format_c"}}"#;
        assert!(decode(bad).is_err());
        assert!(decode("[]").is_err());
        assert!(decode("not json").is_err());
    }

    #[test]
    fn refuses_a_hostile_ruleset() {
        let raw = r#"{"v":1,"id":"a","ts":1.0,"payload":{"type":"rules_update",
          "ruleset":{"revision":-5}}}"#;
        assert!(decode(raw).is_err());
    }

    #[test]
    fn stream_mode_parses_case_insensitively() {
        assert_eq!(StreamMode::parse(" HiRes "), Some(StreamMode::Hires));
        assert_eq!(StreamMode::parse("nope"), None);
    }
}
