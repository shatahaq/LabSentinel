//! Find the dashboard on the LAN by UDP broadcast, as
//! `shared/labsentinel_shared/discovery.py` does.
//!
//! The agent sends a probe from a private port (to the broadcast address and to
//! 127.0.0.1, for a dashboard on this very PC) and also listens on the shared
//! discovery port for the beacons dashboards broadcast every few seconds.

use std::net::{Ipv4Addr, SocketAddr, SocketAddrV4};
use std::time::Duration;

use serde_json::Value;
use socket2::{Domain, Protocol, Socket, Type};
use tokio::net::UdpSocket;
use tokio::time::{sleep_until, Instant};

pub const SERVICE_ID: &str = "labsentinel";
pub const DISCOVERY_VERSION: i64 = 1;
const PROBE_INTERVAL: Duration = Duration::from_millis(1500);

#[derive(Clone, Debug, PartialEq)]
pub struct Beacon {
    pub ws_host: String,
    pub ws_port: u16,
    pub dashboard_name: String,
    pub tls: bool,
}

impl Beacon {
    pub fn ws_url(&self) -> String {
        let scheme = if self.tls { "wss" } else { "ws" };
        format!("{scheme}://{}:{}", self.ws_host, self.ws_port)
    }
}

/// Parse a datagram into a beacon, or `None` if it is not one of ours.
pub fn parse_beacon(data: &[u8]) -> Option<Beacon> {
    let obj: Value = serde_json::from_slice(data).ok()?;
    let obj = obj.as_object()?;
    if obj.get("service")?.as_str()? != SERVICE_ID || obj.get("kind")?.as_str()? != "beacon" {
        return None;
    }
    if obj.get("v")?.as_i64()? != DISCOVERY_VERSION {
        return None;
    }
    let host = match obj.get("ws_host")? {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    let port = match obj.get("ws_port")? {
        Value::Number(n) => n.as_i64()?,
        Value::String(s) => s.trim().parse().ok()?,
        _ => return None,
    };
    if host.is_empty() || !(1..65536).contains(&port) {
        return None;
    }
    Some(Beacon {
        ws_host: host,
        ws_port: port as u16,
        dashboard_name: obj
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or("Dashboard")
            .to_string(),
        tls: obj.get("tls").and_then(Value::as_bool).unwrap_or(false),
    })
}

fn probe_bytes() -> Vec<u8> {
    serde_json::json!({"service": SERVICE_ID, "kind": "probe", "v": DISCOVERY_VERSION})
        .to_string()
        .into_bytes()
}

fn udp_socket(port: u16, shared: bool) -> std::io::Result<UdpSocket> {
    let sock = Socket::new(Domain::IPV4, Type::DGRAM, Some(Protocol::UDP))?;
    if shared {
        sock.set_reuse_address(true)?;
    }
    sock.set_broadcast(true)?;
    sock.bind(&SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, port)).into())?;
    sock.set_nonblocking(true)?;
    UdpSocket::from_std(sock.into())
}

/// Probe and listen until a valid beacon arrives or `timeout` passes.
pub async fn discover(port: u16, timeout: Duration) -> Option<Beacon> {
    let probe = match udp_socket(0, false) {
        Ok(s) => s,
        Err(e) => {
            log::warn!("discovery: cannot open a UDP socket: {e}");
            return None;
        }
    };
    let payload = probe_bytes();
    let send_probes = |sock: &UdpSocket| {
        for target in [Ipv4Addr::BROADCAST, Ipv4Addr::LOCALHOST] {
            let _ = sock.try_send_to(&payload, SocketAddr::V4(SocketAddrV4::new(target, port)));
        }
    };
    // Probe before taking the shared port, so a probe to 127.0.0.1 is never
    // delivered back to our own listener.
    send_probes(&probe);
    // Held exclusively by a dashboard on this PC: then the probe answer is enough.
    let listen = udp_socket(port, true).ok();
    log::debug!("looking for a dashboard on UDP {port} (shared port: {})", listen.is_some());

    let deadline = Instant::now() + timeout;
    let mut next_probe = Instant::now() + PROBE_INTERVAL;
    let mut buf_a = [0u8; 2048];
    let mut buf_b = [0u8; 2048];
    loop {
        let now = Instant::now();
        if now >= deadline {
            return None;
        }
        if now >= next_probe {
            send_probes(&probe);
            next_probe = now + PROBE_INTERVAL;
        }
        let wake = next_probe.min(deadline);
        let received = tokio::select! {
            r = probe.recv_from(&mut buf_a) => r.ok().map(|(n, _)| buf_a[..n].to_vec()),
            r = async {
                match &listen {
                    Some(sock) => sock.recv_from(&mut buf_b).await,
                    None => std::future::pending().await,
                }
            } => r.ok().map(|(n, _)| buf_b[..n].to_vec()),
            _ = sleep_until(wake) => None,
        };
        // Errors (Windows reports "port unreachable" for a probe nobody
        // answered) just mean: keep listening.
        if let Some(data) = received {
            if let Some(beacon) = parse_beacon(&data) {
                return Some(beacon);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_the_dashboard_beacon() {
        let b = parse_beacon(
            br#"{"service":"labsentinel","kind":"beacon","v":1,"ws_host":"10.0.0.5","ws_port":8765,"name":"Lab A","tls":false}"#,
        )
        .unwrap();
        assert_eq!(b.ws_url(), "ws://10.0.0.5:8765");
        assert_eq!(b.dashboard_name, "Lab A");
    }

    #[test]
    fn ignores_foreign_or_broken_datagrams() {
        assert!(parse_beacon(b"hello").is_none());
        assert!(parse_beacon(br#"{"service":"other","kind":"beacon","v":1}"#).is_none());
        assert!(parse_beacon(br#"{"service":"labsentinel","kind":"probe","v":1}"#).is_none());
        assert!(parse_beacon(
            br#"{"service":"labsentinel","kind":"beacon","v":1,"ws_host":"h","ws_port":0}"#
        )
        .is_none());
        assert!(parse_beacon(
            br#"{"service":"labsentinel","kind":"beacon","v":2,"ws_host":"h","ws_port":1}"#
        )
        .is_none());
    }

    #[tokio::test]
    async fn finds_a_dashboard_that_answers_probes() {
        // A fake dashboard on an unused port answers the first probe it gets.
        let server = udp_socket(0, false).unwrap();
        let port = server.local_addr().unwrap().port();
        tokio::spawn(async move {
            let mut buf = [0u8; 2048];
            if let Ok((_, from)) = server.recv_from(&mut buf).await {
                let beacon = br#"{"service":"labsentinel","kind":"beacon","v":1,"ws_host":"127.0.0.1","ws_port":8765,"name":"Uji"}"#;
                let _ = server.send_to(beacon, from).await;
            }
        });
        let found = discover(port, Duration::from_secs(5)).await.unwrap();
        assert_eq!(found.dashboard_name, "Uji");
    }
}
