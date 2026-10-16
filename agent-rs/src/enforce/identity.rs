//! Process snapshot model and a SHA-256 cache keyed by (path, size, mtime), so
//! a stable executable is hashed once and a changed one is hashed again.

use std::collections::HashMap;
use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::time::SystemTime;

use sha2::{Digest, Sha256};

/// Windows pseudo-accounts that own system processes; never interactive users.
pub const SYSTEM_ACCOUNTS: &[&str] = &[
    "system",
    "local service",
    "network service",
    "nt authority\\system",
    "nt authority\\local service",
    "nt authority\\network service",
    "",
];

/// One process as the agent sees it.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct ProcessInfo {
    pub pid: u32,
    /// Executable basename, lowercased (e.g. "chrome.exe").
    pub name: String,
    pub exe_path: Option<String>,
    /// Owner; may be "LAB\\student" or a system account. `None` = unknown.
    pub username: Option<String>,
    pub session_id: Option<u32>,
    pub has_visible_window: bool,
    pub window_title: String,
    /// Owned by the interactive user at the console.
    pub is_current_user: bool,
    /// Runs with administrator rights: a participant cannot start one.
    pub elevated: bool,
}

impl ProcessInfo {
    pub fn normalized_owner(&self) -> String {
        self.username.as_deref().unwrap_or("").trim().to_lowercase()
    }

    pub fn is_system_owned(&self) -> bool {
        SYSTEM_ACCOUNTS.contains(&self.normalized_owner().as_str())
    }
}

/// Don't hash absurdly large files completely.
const MAX_HASH_BYTES: u64 = 512 * 1024 * 1024;

#[derive(Default)]
pub struct HashCache {
    cache: HashMap<String, (u64, Option<SystemTime>, String)>,
}

impl HashCache {
    /// The file's SHA-256 (lowercase hex), from cache when still fresh.
    pub fn sha256(&mut self, path: &str) -> Option<String> {
        let meta = std::fs::metadata(path).ok()?;
        let (size, mtime) = (meta.len(), meta.modified().ok());
        if let Some((s, m, digest)) = self.cache.get(path) {
            if *s == size && *m == mtime {
                return Some(digest.clone());
            }
        }
        let digest = compute(Path::new(path), size)?;
        self.cache.insert(path.to_string(), (size, mtime, digest.clone()));
        Some(digest)
    }
}

fn compute(path: &Path, size: u64) -> Option<String> {
    let mut fh = File::open(path).ok()?;
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; 1024 * 1024];
    let mut read: u64 = 0;
    loop {
        let n = fh.read(&mut buf).ok()?;
        if n == 0 {
            break;
        }
        read += n as u64;
        if read > MAX_HASH_BYTES {
            // Same discriminator as the Python agent: the first part plus the size.
            hasher.update(size.to_string().as_bytes());
            break;
        }
        hasher.update(&buf[..n]);
    }
    Some(crate::util::hex(&hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::util::random_hex;

    #[test]
    fn hashes_and_rehashes_on_change() {
        let path = std::env::temp_dir().join(format!("ls-hash-{}.bin", random_hex(4)));
        std::fs::write(&path, b"abc").unwrap();
        let p = path.to_str().unwrap();
        let mut cache = HashCache::default();
        let first = cache.sha256(p).unwrap();
        assert_eq!(first, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
        std::fs::write(&path, b"abcd").unwrap();
        assert_ne!(cache.sha256(p).unwrap(), first);
        let _ = std::fs::remove_file(&path);
        assert!(cache.sha256("/no/such/file").is_none());
    }

    #[test]
    fn system_accounts_are_recognised() {
        let p = ProcessInfo { username: Some("NT AUTHORITY\\SYSTEM".into()), ..Default::default() };
        assert!(p.is_system_owned());
        let p = ProcessInfo { username: None, ..Default::default() };
        assert!(p.is_system_owned(), "unknown owner counts as system: leave it alone");
        let p = ProcessInfo { username: Some("LAB\\andi".into()), ..Default::default() };
        assert!(!p.is_system_owned());
    }
}
