"""Print screenshots into the job log as base64 JPEG, for reading the log alone.

    python packaging/ci/emit_images.py DIR
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from PIL import Image

CHUNK = 6000

for path in sorted(Path(sys.argv[1]).glob("*.png")):
    img = Image.open(path).convert("RGB")
    if img.width > 1366:
        img = img.resize((1366, int(img.height * 1366 / img.width)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72)
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    print(f"LSIMG-BEGIN {path.stem} {len(data)}")
    for i in range(0, len(data), CHUNK):
        print(f"LSIMG {data[i:i + CHUNK]}")
    print(f"LSIMG-END {path.stem}")
