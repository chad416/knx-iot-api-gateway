"""Start the gateway in demo mode for each design and take PNG screenshots of the web client.

    cd web && npm install && npm run build && cd ..
    pip install playwright            (uses the installed Edge, no browser download)
    python scripts/take_screenshots.py

The screenshots are of the running client talking to the running gateway over HTTP and
WebSocket. Bus time runs at 0.1x so that a race is visible on screen.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "img")


def wait_up(port: int) -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/.well-known/knx", timeout=1).read()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def main() -> None:
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        for i, design in enumerate(("optimistic", "confirmed", "versioned")):
            port = 8091 + i
            srv = subprocess.Popen([sys.executable, "-m", "knxgw.gateway.server", "--demo", "--design", design,
                                    "--speed", "0.1", "--port", str(port)], cwd=ROOT)
            try:
                wait_up(port)
                page = browser.new_page(viewport={"width": 1480, "height": 1000})
                page.goto(f"http://127.0.0.1:{port}/")
                page.wait_for_selector(".room")
                # a blind on its way, and a race with the wall switch 20 ms (bus time) after the API write
                page.locator(".room").nth(1).get_by_text("API: 100 %").last.click()
                page.locator(".room").nth(2).get_by_text("API: 60 %").first.click()
                page.fill("input[type=range]", "20")
                page.click("#race")
                page.wait_for_timeout(900)            # mid-race: 90 ms of bus time
                page.screenshot(path=os.path.join(OUT, f"client_{design}_during.png"))
                page.wait_for_timeout(6000)
                page.screenshot(path=os.path.join(OUT, f"client_{design}_after.png"))
                page.close()
            finally:
                srv.terminate()
                srv.wait(timeout=10)
        browser.close()
    print("screenshots in", OUT)


if __name__ == "__main__":
    main()
