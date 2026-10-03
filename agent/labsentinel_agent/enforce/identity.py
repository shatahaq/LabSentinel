"""Process identity model and a cheap, correct SHA-256 cache.

``ProcessInfo`` is a plain snapshot of one process as the agent sees it —
produced by the platform layer, consumed by the pure safety/matcher logic.

``HashCache`` avoids re-hashing the same executable repeatedly: it keys cached
digests by (path, size, mtime_ns) so a file that changes on disk is re-hashed,
but a stable binary is hashed once. This keeps CPU low (NFR target < 2%).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

# Windows pseudo-accounts that own system processes; never interactive users.
SYSTEM_ACCOUNTS: frozenset[str] = frozenset(
    {
        "system",
        "local service",
        "network service",
        "nt authority\\system",
        "nt authority\\local service",
        "nt authority\\network service",
        "",
    }
)


@dataclass(frozen=True)
class ProcessInfo:
    """Immutable snapshot of a process, as observed by the platform layer."""

    pid: int
    name: str  # executable basename, lowercased (e.g. "chrome.exe")
    exe_path: str | None = None
    username: str | None = None  # owner; may be "LAB\\student" or a system account
    session_id: int | None = None  # Windows session; interactive user == console
    has_visible_window: bool = False
    window_title: str = ""
    is_current_user: bool = False  # owned by the logged-in interactive user

    def normalized_owner(self) -> str:
        return (self.username or "").strip().lower()

    def is_system_owned(self) -> bool:
        return self.normalized_owner() in SYSTEM_ACCOUNTS


@dataclass
class HashCache:
    """Caches SHA-256 of executables keyed by (path, size, mtime_ns)."""

    _cache: dict[str, tuple[int, int, str]] = field(default_factory=dict)
    max_bytes: int = 512 * 1024 * 1024  # don't hash absurdly large files fully

    def sha256(self, path: str | os.PathLike[str]) -> str | None:
        """Return the file's SHA-256, using the cache when fresh. None on error."""
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return None
        key = str(p)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]
        digest = self._compute(p, st.st_size)
        if digest is not None:
            self._cache[key] = (st.st_size, st.st_mtime_ns, digest)
        return digest

    def _compute(self, p: Path, size: int) -> str | None:
        h = hashlib.sha256()
        try:
            with p.open("rb") as fh:
                read = 0
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    read += len(chunk)
                    if read > self.max_bytes:
                        # Hash the first max_bytes + the size as a discriminator.
                        h.update(str(size).encode())
                        break
                    h.update(chunk)
        except OSError:
            return None
        return h.hexdigest()
