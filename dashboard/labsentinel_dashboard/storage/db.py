"""SQLite storage for agents, violations, commands, sessions, rules and audit.

One ``Database`` object is shared by the WebSocket loop thread and the Eel UI
thread, so every access goes through a lock on a single connection opened with
``check_same_thread=False`` in WAL mode. All SQL is parameterized. Evidence
screenshots are stored as files on disk (path in the DB), not as BLOBs, so the
database stays small and retention can delete files and rows together.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    hostname      TEXT NOT NULL,
    username      TEXT NOT NULL DEFAULT '',
    agent_version TEXT NOT NULL DEFAULT '',
    remote_addr   TEXT NOT NULL DEFAULT '',
    group_name    TEXT NOT NULL DEFAULT '',
    first_seen    REAL NOT NULL,
    last_seen     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,          -- scheduled | active | ended | cancelled
    scheduled_start REAL,
    scheduled_end   REAL,
    started_at      REAL,
    ended_at        REAL,
    created_by      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS violations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER REFERENCES sessions(id),
    agent_id            TEXT NOT NULL,
    hostname            TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL,
    subject             TEXT NOT NULL,
    detail              TEXT NOT NULL DEFAULT '',
    detected_from_title INTEGER NOT NULL DEFAULT 0,
    dry_run             INTEGER NOT NULL DEFAULT 0,
    occurred_at         REAL NOT NULL,
    received_at         REAL NOT NULL,
    screenshot_path     TEXT
);
CREATE INDEX IF NOT EXISTS ix_violations_time ON violations(occurred_at);
CREATE INDEX IF NOT EXISTS ix_violations_session ON violations(session_id);

CREATE TABLE IF NOT EXISTS commands (
    id              TEXT PRIMARY KEY,       -- envelope id == ack_id
    agent_id        TEXT NOT NULL,
    command         TEXT NOT NULL,
    issued_by       TEXT NOT NULL,
    issued_at       REAL NOT NULL,
    status          TEXT NOT NULL,          -- pending | acked | failed | timeout | unreachable
    acked_at        REAL,
    resulting_state TEXT,
    message         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_commands_time ON commands(issued_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '{}',
    result  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_audit_time ON audit_log(ts);

CREATE TABLE IF NOT EXISTS rulesets (
    revision   INTEGER PRIMARY KEY,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL,
    created_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ViolationRow:
    id: int
    session_id: int | None
    agent_id: str
    hostname: str
    kind: str
    subject: str
    detail: str
    detected_from_title: bool
    dry_run: bool
    occurred_at: float
    received_at: float
    screenshot_path: str | None


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: float
    actor: str
    action: str
    target: str
    details: dict[str, Any]
    result: str


@dataclass(frozen=True)
class SessionRow:
    id: int
    name: str
    mode: str
    status: str
    scheduled_start: float | None
    scheduled_end: float | None
    started_at: float | None
    ended_at: float | None
    created_by: str


class Database:
    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if self._path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    # -- agents ---------------------------------------------------------------
    def upsert_agent(
        self,
        *,
        agent_id: str,
        hostname: str,
        username: str,
        agent_version: str,
        remote_addr: str,
        now: float | None = None,
    ) -> None:
        ts = time.time() if now is None else now
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO agents(agent_id, hostname, username, agent_version, remote_addr,
                                   first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    hostname = excluded.hostname,
                    username = excluded.username,
                    agent_version = excluded.agent_version,
                    remote_addr = excluded.remote_addr,
                    last_seen = excluded.last_seen
                """,
                (agent_id, hostname, username, agent_version, remote_addr, ts, ts),
            )

    def touch_agent(self, agent_id: str, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        with self._tx() as c:
            c.execute("UPDATE agents SET last_seen = ? WHERE agent_id = ?", (ts, agent_id))

    def set_agent_group(self, agent_id: str, group_name: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE agents SET group_name = ? WHERE agent_id = ?", (group_name, agent_id)
            )

    def list_agents(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("SELECT * FROM agents ORDER BY hostname")]

    # -- violations -----------------------------------------------------------
    def add_violation(
        self,
        *,
        agent_id: str,
        hostname: str,
        kind: str,
        subject: str,
        detail: str,
        detected_from_title: bool,
        dry_run: bool,
        occurred_at: float,
        screenshot_path: str | None,
        session_id: int | None,
        received_at: float | None = None,
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO violations(session_id, agent_id, hostname, kind, subject, detail,
                    detected_from_title, dry_run, occurred_at, received_at, screenshot_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_id,
                    hostname,
                    kind,
                    subject,
                    detail,
                    int(detected_from_title),
                    int(dry_run),
                    occurred_at,
                    time.time() if received_at is None else received_at,
                    screenshot_path,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_violations(
        self,
        *,
        session_id: int | None = None,
        since: float | None = None,
        limit: int = 500,
    ) -> list[ViolationRow]:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if since is not None:
            clauses.append("occurred_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        rows = self._query(
            f"SELECT * FROM violations {where} ORDER BY occurred_at DESC LIMIT ?", tuple(params)
        )
        return [
            ViolationRow(
                id=r["id"],
                session_id=r["session_id"],
                agent_id=r["agent_id"],
                hostname=r["hostname"],
                kind=r["kind"],
                subject=r["subject"],
                detail=r["detail"],
                detected_from_title=bool(r["detected_from_title"]),
                dry_run=bool(r["dry_run"]),
                occurred_at=r["occurred_at"],
                received_at=r["received_at"],
                screenshot_path=r["screenshot_path"],
            )
            for r in rows
        ]

    # -- commands -------------------------------------------------------------
    def add_command(
        self, *, command_id: str, agent_id: str, command: str, issued_by: str, issued_at: float
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO commands(id, agent_id, command, issued_by, issued_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (command_id, agent_id, command, issued_by, issued_at),
            )

    def finish_command(
        self,
        command_id: str,
        *,
        status: str,
        resulting_state: str | None = None,
        message: str = "",
        at: float | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                UPDATE commands SET status = ?, resulting_state = ?, message = ?, acked_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, resulting_state, message, time.time() if at is None else at, command_id),
            )

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM commands WHERE id = ?", (command_id,))
        return dict(rows[0]) if rows else None

    def recent_commands(
        self, *, agent_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if agent_id is None:
            rows = self._query("SELECT * FROM commands ORDER BY issued_at DESC LIMIT ?", (limit,))
        else:
            rows = self._query(
                "SELECT * FROM commands WHERE agent_id = ? ORDER BY issued_at DESC LIMIT ?",
                (agent_id, limit),
            )
        return [dict(r) for r in rows]

    # -- audit ----------------------------------------------------------------
    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str = "",
        details: dict[str, Any] | None = None,
        result: str = "",
        ts: float | None = None,
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO audit_log(ts, actor, action, target, details, result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    time.time() if ts is None else ts,
                    actor,
                    action,
                    target,
                    json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                    result,
                ),
            )
            return int(cur.lastrowid or 0)

    def list_audit(self, *, limit: int = 500) -> list[AuditRow]:
        rows = self._query("SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
        return [
            AuditRow(
                id=r["id"],
                ts=r["ts"],
                actor=r["actor"],
                action=r["action"],
                target=r["target"],
                details=json.loads(r["details"]),
                result=r["result"],
            )
            for r in rows
        ]

    # -- sessions -------------------------------------------------------------
    def create_session(
        self,
        *,
        name: str,
        mode: str,
        status: str,
        created_by: str,
        scheduled_start: float | None = None,
        scheduled_end: float | None = None,
        started_at: float | None = None,
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO sessions(name, mode, status, scheduled_start, scheduled_end,
                                     started_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, mode, status, scheduled_start, scheduled_end, started_at, created_by),
            )
            return int(cur.lastrowid or 0)

    def set_session_status(
        self,
        session_id: int,
        status: str,
        *,
        started_at: float | None = None,
        ended_at: float | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                UPDATE sessions SET status = ?,
                    started_at = COALESCE(?, started_at),
                    ended_at = COALESCE(?, ended_at)
                WHERE id = ?
                """,
                (status, started_at, ended_at, session_id),
            )

    def get_session(self, session_id: int) -> SessionRow | None:
        rows = self._query("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return _session_row(rows[0]) if rows else None

    def list_sessions(self, *, status: str | None = None, limit: int = 200) -> list[SessionRow]:
        if status is None:
            rows = self._query("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = self._query(
                "SELECT * FROM sessions WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            )
        return [_session_row(r) for r in rows]

    # -- rulesets -------------------------------------------------------------
    def save_ruleset(self, *, revision: int, body: str, created_by: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO rulesets(revision, body, created_at, created_by) VALUES (?, ?, ?, ?)",
                (revision, body, time.time(), created_by),
            )

    def latest_ruleset(self) -> str | None:
        rows = self._query("SELECT body FROM rulesets ORDER BY revision DESC LIMIT 1")
        return str(rows[0]["body"]) if rows else None

    # -- settings -------------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    # -- retention ------------------------------------------------------------
    def purge_older_than(self, cutoff: float) -> list[str]:
        """Delete old violations/commands/audit; return screenshot paths to delete."""
        with self._tx() as c:
            paths = [
                str(r["screenshot_path"])
                for r in c.execute(
                    "SELECT screenshot_path FROM violations "
                    "WHERE occurred_at < ? AND screenshot_path IS NOT NULL",
                    (cutoff,),
                ).fetchall()
            ]
            c.execute("DELETE FROM violations WHERE occurred_at < ?", (cutoff,))
            c.execute("DELETE FROM commands WHERE issued_at < ?", (cutoff,))
            c.execute("DELETE FROM audit_log WHERE ts < ?", (cutoff,))
        return paths


def _session_row(r: sqlite3.Row) -> SessionRow:
    return SessionRow(
        id=r["id"],
        name=r["name"],
        mode=r["mode"],
        status=r["status"],
        scheduled_start=r["scheduled_start"],
        scheduled_end=r["scheduled_end"],
        started_at=r["started_at"],
        ended_at=r["ended_at"],
        created_by=r["created_by"],
    )
