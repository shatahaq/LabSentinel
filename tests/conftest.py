"""Make the three source roots importable without an install step.

Keeps `pytest` runnable straight from a clean checkout (and in CI) before
`pip install -e .` has happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _pkg_root in ("shared", "agent", "dashboard"):
    _p = str(_ROOT / _pkg_root)
    if _p not in sys.path:
        sys.path.insert(0, _p)
