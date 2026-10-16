//! Agent configuration from `agent.toml`, the same file and keys the Python
//! agent reads (`shared/labsentinel_shared/config.py`), with the same defaults.

use std::path::{Path, PathBuf};

use serde::Deserialize;

pub const DEFAULT_DISCOVERY_PORT: u16 = 50123;

fn default_data_dir() -> PathBuf {
    PathBuf::from("C:/ProgramData/LabSentinel/agent")
}
fn default_token_file() -> PathBuf {
    PathBuf::from("C:/ProgramData/LabSentinel/agent/token")
}
fn default_log_level() -> String {
    "INFO".into()
}
fn default_discovery_port() -> u16 {
    DEFAULT_DISCOVERY_PORT
}
fn default_discovery_timeout() -> f64 {
    8.0
}
fn default_heartbeat() -> f64 {
    5.0
}
fn default_grace() -> i64 {
    5
}
fn default_poll() -> f64 {
    3.0
}
fn default_grid_fps() -> f64 {
    1.0
}
fn default_hires_fps() -> f64 {
    5.0
}

#[derive(Deserialize, Clone, Debug)]
pub struct AgentConfig {
    #[serde(default = "default_data_dir")]
    pub data_dir: PathBuf,
    #[serde(default = "default_log_level")]
    pub log_level: String,
    #[serde(default = "default_discovery_port")]
    pub discovery_port: u16,
    #[serde(default)]
    pub direct_url: Option<String>,
    #[serde(default = "default_discovery_timeout")]
    pub discovery_timeout_s: f64,
    #[serde(default = "default_token_file")]
    pub token_file: PathBuf,
    #[serde(default = "default_heartbeat")]
    pub heartbeat_interval_s: f64,
    #[serde(default = "default_grace")]
    pub close_grace_seconds: i64,
    #[serde(default = "default_poll")]
    pub poll_fallback_interval_s: f64,
    // Read for compatibility with agent.toml files; the stream profiles are fixed.
    #[serde(default = "default_grid_fps")]
    pub screen_grid_fps: f64,
    #[serde(default = "default_hires_fps")]
    pub screen_hi_res_fps: f64,
}

impl Default for AgentConfig {
    fn default() -> Self {
        toml::from_str("").expect("empty config uses the defaults")
    }
}

impl AgentConfig {
    pub fn rule_cache_path(&self) -> PathBuf {
        self.data_dir.join("ruleset.json")
    }
    pub fn agent_id_path(&self) -> PathBuf {
        self.data_dir.join("agent_id")
    }
    pub fn log_path(&self) -> PathBuf {
        self.data_dir.join("logs").join("agent.log")
    }

    fn validate(self) -> Result<Self, String> {
        let checks = [
            (self.discovery_port > 0, "discovery_port"),
            (self.discovery_timeout_s > 0.0, "discovery_timeout_s"),
            (self.heartbeat_interval_s > 0.0 && self.heartbeat_interval_s <= 60.0, "heartbeat_interval_s"),
            ((0..=60).contains(&self.close_grace_seconds), "close_grace_seconds"),
            (self.poll_fallback_interval_s > 0.0 && self.poll_fallback_interval_s <= 30.0, "poll_fallback_interval_s"),
        ];
        for (ok, name) in checks {
            if !ok {
                return Err(format!("{name} is out of range"));
            }
        }
        Ok(self)
    }
}

/// Load the configuration; a missing file means the defaults.
pub fn load(path: Option<&Path>) -> Result<AgentConfig, String> {
    let Some(path) = path.filter(|p| p.exists()) else {
        return Ok(AgentConfig::default());
    };
    let text = std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let cfg: AgentConfig = toml::from_str(&text).map_err(|e| format!("{}: {e}", path.display()))?;
    cfg.validate()
}

/// `agent.toml` next to the installed executable, if there is one: the
/// service, the tray helper and the command line all read the same file.
pub fn default_path() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let candidate = exe.with_file_name("agent.toml");
    candidate.exists().then_some(candidate)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_match_the_python_agent() {
        let cfg = AgentConfig::default();
        assert_eq!(cfg.data_dir, PathBuf::from("C:/ProgramData/LabSentinel/agent"));
        assert_eq!(cfg.discovery_port, 50123);
        assert_eq!(cfg.heartbeat_interval_s, 5.0);
        assert_eq!(cfg.poll_fallback_interval_s, 3.0);
        assert!(cfg.direct_url.is_none());
    }

    #[test]
    fn reads_an_agent_toml() {
        let cfg: AgentConfig = toml::from_str(
            "data_dir = 'D:/ls'\ndirect_url = 'ws://10.0.0.5:8765'\nlog_level = 'DEBUG'\n",
        )
        .unwrap();
        assert_eq!(cfg.data_dir, PathBuf::from("D:/ls"));
        assert_eq!(cfg.direct_url.as_deref(), Some("ws://10.0.0.5:8765"));
        assert_eq!(cfg.rule_cache_path(), PathBuf::from("D:/ls/ruleset.json"));
    }

    #[test]
    fn rejects_out_of_range_values() {
        let cfg: AgentConfig = toml::from_str("heartbeat_interval_s = 0").unwrap();
        assert!(cfg.validate().is_err());
    }
}
