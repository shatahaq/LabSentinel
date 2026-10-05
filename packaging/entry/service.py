"""Frozen entry point: labsentinel-service.exe (the Windows Service host)."""

import sys

from labsentinel_agent.service import main

if __name__ == "__main__":
    sys.exit(main())
