"""Photograph the dashboard showing this PC's screen (Windows CI only).

    python packaging/ci/dashboard_shots.py OUT_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 768})
        page.goto("http://localhost:8080/index.html#monitoring")
        page.wait_for_timeout(2000)
        if page.is_visible(".dialog"):
            page.click(".dialog__foot .btn--primary")
        page.wait_for_selector(".pc__screen img", timeout=60000)
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / "5-dashboard-layar-pc.png"))
        print("card:", page.locator(".pc").first.inner_text().replace("\n", " | "))
        page.click(".pc")
        page.wait_for_timeout(5000)
        page.screenshot(path=str(out / "6-dashboard-detail.png"))
        page.goto("http://localhost:8080/index.html#laporan")
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out / "7-dashboard-laporan.png"))
        page.goto("http://localhost:8080/index.html#aktivitas")
        page.wait_for_timeout(3000)
        page.screenshot(path=str(out / "8-dashboard-aktivitas.png"))
        browser.close()


if __name__ == "__main__":
    main()
