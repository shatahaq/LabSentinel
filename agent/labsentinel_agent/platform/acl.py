"""Restrict a file to SYSTEM and Administrators on Windows.

Used for the agent's credential. ``chmod`` does nothing useful on Windows; the
file's ACL decides who can read it, and by default anything under ProgramData
is readable by every local user, participants included.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("labsentinel.agent.acl")

# Well-known SIDs, so this is right on any Windows display language
# (the group is not called "Administrators" everywhere).
SID_SYSTEM = "*S-1-5-18"
SID_ADMINISTRATORS = "*S-1-5-32-544"


def restrict_to_admins_cmd(path: Path) -> list[str]:
    """icacls command: drop inherited access, grant only SYSTEM and Administrators."""
    return [
        "icacls", str(path), "/inheritance:r",
        "/grant:r", f"{SID_SYSTEM}:F", "/grant:r", f"{SID_ADMINISTRATORS}:F",
    ]


def restrict_to_admins(path: Path) -> bool:
    """Best effort. Returns False (and logs) when it could not be applied."""
    if sys.platform != "win32" or not path.exists():
        return False
    try:
        subprocess.run(
            restrict_to_admins_cmd(path), check=True, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        log.warning("could not restrict access to %s", path, exc_info=True)
        return False
