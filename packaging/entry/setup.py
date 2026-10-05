"""Frozen entry point: labsentinel-setup.exe (windowed; the pairing window)."""

import sys

from labsentinel_agent.gui.setup_window import main

if __name__ == "__main__":
    sys.exit(main())
