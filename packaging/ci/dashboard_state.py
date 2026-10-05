"""Read the installed dashboard's state for the Windows end-to-end CI job.

    python packaging/ci/dashboard_state.py code             # print the pairing code
    python packaging/ci/dashboard_state.py check-connected  # PC paired + connected
    python packaging/ci/dashboard_state.py check-tamper     # forced kill was reported

Reads the SQLite database directly (read-only) so the check does not depend on
the admin UI. Exits non-zero, with the reason, when a check fails.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB = Path(r"C:\ProgramData\LabSentinel\dashboard\labsentinel.sqlite")
AGENT_LOG = Path(r"C:\ProgramData\LabSentinel\agent\logs\agent.log")
WORKER_LOG = Path(r"C:\ProgramData\LabSentinel\agent\logs\desktop-worker.log")


def query(sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def wait_for(what: str, check: object, timeout: float = 90.0) -> object:
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = check()  # type: ignore[operator]
        except sqlite3.Error as exc:
            result = None
            print(f"  (db not ready: {exc})")
        if result:
            return result
        if time.monotonic() > deadline:
            raise SystemExit(f"FAIL: {what} (waited {timeout:.0f}s)")
        time.sleep(3)


def log_lines(path: Path, *needles: str) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [ln for ln in text if any(n in ln for n in needles)]


def cmd_code() -> None:
    row = wait_for("dashboard created a pairing code",
                   lambda: query("SELECT value FROM settings WHERE key = 'pairing_code'"))
    print(json.loads(row[0]["value"]))  # type: ignore[index]


def cmd_check_connected() -> None:
    paired = wait_for("agent paired with the dashboard",
                      lambda: query("SELECT agent_id FROM agent_tokens"))
    print("paired:", [r["agent_id"] for r in paired])  # type: ignore[union-attr]
    agents = wait_for("agent connected to the dashboard",
                      lambda: query("SELECT agent_id, hostname, username, agent_version "
                                    "FROM agents"))
    for r in agents:  # type: ignore[union-attr]
        print("connected:", dict(r))
    # The service runs as SYSTEM; the dashboard must name the participant.
    named = wait_for("the signed-in user shown for the PC",
                     lambda: query("SELECT username FROM agents "
                                   "WHERE username != '' AND upper(username) != 'SYSTEM'"))
    print("participant shown as:", [r["username"] for r in named])  # type: ignore[union-attr]

    # Desktop worker: the part only a real Windows session can show.
    worker = log_lines(AGENT_LOG, "desktop worker", "desktop access")
    print("\n".join(worker[-10:]) or "agent log has no desktop worker lines")
    print("\n".join(log_lines(WORKER_LOG, "")[-10:]) or "no desktop-worker.log")

    # A process the default rules do not allow, in the logged-in session: in
    # dry-run (the default) it is recorded, with a screenshot taken by the
    # worker, but not closed.
    notepad = subprocess.Popen(["notepad.exe"])
    try:
        deadline = time.monotonic() + 45
        rows: list[sqlite3.Row] = []
        while time.monotonic() < deadline and not rows:
            time.sleep(3)
            rows = query("SELECT kind, subject, dry_run, screenshot_path FROM violations "
                         "WHERE subject = 'notepad.exe'")
        if rows:
            row = dict(rows[0])
            print("violation recorded:", row)
            shot = row.get("screenshot_path")
            if shot and Path(shot).exists():
                print(f"evidence screenshot: {shot} ({Path(shot).stat().st_size} bytes)")
            else:
                print("WARN: violation recorded without a screenshot")
        else:
            print("WARN: notepad.exe was not recorded as a violation within 45 s "
                  "(desktop worker or session detection did not see it)")
    finally:
        notepad.kill()


def cmd_check_tamper() -> None:
    rows = wait_for("forced kill reported as agent_tamper",
                    lambda: query("SELECT subject, detail FROM violations "
                                  "WHERE kind = 'agent_tamper'"), timeout=90)
    for r in rows:  # type: ignore[union-attr]
        print("tamper:", dict(r))


if __name__ == "__main__":
    commands = {"code": cmd_code, "check-connected": cmd_check_connected,
                "check-tamper": cmd_check_tamper}
    commands[sys.argv[1]]()
