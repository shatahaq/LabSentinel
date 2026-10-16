//! Small helpers shared by every module: time, randomness, text limits, files.

use std::fs;
use std::io::{self, Write};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

/// Seconds since the Unix epoch, as the wire protocol carries timestamps.
pub fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `n` random bytes from the operating system's CSPRNG.
pub fn random_bytes(n: usize) -> Vec<u8> {
    let mut buf = vec![0u8; n];
    getrandom::fill(&mut buf).expect("operating system random source unavailable");
    buf
}

/// Lowercase hex of `n` random bytes (an envelope id is 16 bytes, like uuid4().hex).
pub fn random_hex(n: usize) -> String {
    hex(&random_bytes(n))
}

/// URL-safe base64 of `n` random bytes, like Python's `secrets.token_urlsafe`.
pub fn token_urlsafe(n: usize) -> String {
    use base64::Engine;
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(random_bytes(n))
}

pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 0x0f) as usize] as char);
    }
    out
}

/// Cut `s` to at most `max` characters (the protocol's limits count characters,
/// not bytes, exactly like pydantic's `max_length`).
pub fn truncate_chars(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        Some((idx, _)) => s[..idx].to_string(),
        None => s.to_string(),
    }
}

/// Constant-time comparison for secrets (tokens).
pub fn secure_eq(a: &str, b: &str) -> bool {
    let (a, b) = (a.as_bytes(), b.as_bytes());
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

/// Write a file so that readers see either the old or the new content, never
/// half of it: write a temporary file next to it, flush it to disk, rename.
pub fn atomic_write(path: &Path, data: &[u8]) -> io::Result<()> {
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(dir)?;
    let tmp = dir.join(format!(
        ".{}.{}.tmp",
        path.file_name().and_then(|n| n.to_str()).unwrap_or("file"),
        random_hex(4)
    ));
    let result = (|| {
        let mut fh = fs::File::create(&tmp)?;
        fh.write_all(data)?;
        fh.sync_all()?;
        drop(fh);
        fs::rename(&tmp, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

/// Read a small text file and trim it; `None` if missing or empty.
pub fn read_trimmed(path: &Path) -> Option<String> {
    let text = fs::read_to_string(path).ok()?;
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncates_by_characters_not_bytes() {
        assert_eq!(truncate_chars("héllo", 2), "hé");
        assert_eq!(truncate_chars("abc", 10), "abc");
        assert_eq!(truncate_chars("—•—", 1), "—");
    }

    #[test]
    fn ids_and_tokens_have_the_expected_shape() {
        let id = random_hex(16);
        assert_eq!(id.len(), 32);
        assert!(id.chars().all(|c| c.is_ascii_hexdigit()));
        let token = token_urlsafe(32);
        assert_eq!(token.len(), 43);
        assert_ne!(token, token_urlsafe(32));
    }

    #[test]
    fn secure_eq_compares_whole_strings() {
        assert!(secure_eq("abc", "abc"));
        assert!(!secure_eq("abc", "abd"));
        assert!(!secure_eq("abc", "abcd"));
    }

    #[test]
    fn atomic_write_replaces_content() {
        let dir = std::env::temp_dir().join(format!("ls-util-{}", random_hex(4)));
        let path = dir.join("f.txt");
        atomic_write(&path, b"one").unwrap();
        atomic_write(&path, b"two").unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), "two");
        assert_eq!(fs::read_dir(&dir).unwrap().count(), 1, "no temp file left behind");
        let _ = fs::remove_dir_all(dir);
    }
}
