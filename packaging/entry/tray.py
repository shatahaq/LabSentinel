"""Frozen entry point: labsentinel-tray.exe (windowless, one per logged-in user)."""

import sys

from labsentinel_agent.tray.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
