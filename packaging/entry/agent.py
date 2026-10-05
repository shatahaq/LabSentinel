"""Frozen entry point: labsentinel-agent.exe (pairing, diagnostics, console run)."""

import sys

from labsentinel_agent.app import main

if __name__ == "__main__":
    sys.exit(main())
