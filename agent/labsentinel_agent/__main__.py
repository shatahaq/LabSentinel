"""``python -m labsentinel_agent`` entry point."""

from __future__ import annotations

import sys

from labsentinel_agent.app import main

if __name__ == "__main__":
    sys.exit(main())
