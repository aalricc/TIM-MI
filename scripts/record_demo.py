"""Record the live demo: the MI dashboard while MI recovers simulated outages.

Needs the full stack up with test hooks (`TARGET_TEST_HOOKS=1 docker compose up`) and
Playwright with a Chromium/Chrome (`pip install playwright`). Captions are drawn on the
recorded page for the viewer only; MI never sees them. The fault is sent to the target's
test hook, exactly as the harness campaign does, so MI only observes its symptoms.

    python scripts/record_demo.py --out demo.mp4
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

CAPTION_JS = """
(text) => {
  let el = document.getElementById('demo-caption');
  if (!el) {
    el = document.createElement('div');
    el.id = 'demo-caption';
    el.style.cssText = 'position:fixed;right:20px;top:14px;' +
      'z-index:9999;max-width:820px;padding:12px 20px;border-radius:12px;' +
      'background:rgba(8,30,27,.94);color:#e6fffa;border:1px solid #3cc7b5;' +
      'font:600 20px/1.35 system-ui,sans-serif;text-align:center;' +
      'box-shadow:0 12px 40px rgba(0,0,0,.5);transition:opacity .3s';
    document.body.appendChild(el);
  }
  el.style.opacity = text ? '1' : '0';
  el.textContent = text || '';
}
"""


def _get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - local demo URL
        data = json.loads(response.read())
    return data if isinstance(data, dict) else {}


def _post(url: str) -> None:
    request = urllib.request.Request(url, method="POST")  # noqa: S310 - local demo URL
    with urllib.request.urlopen(request, timeout=10):  # noqa: S310 - local demo URL
        pass


def _caption(page: Page, text: str) -> None:
    page.evaluate(CAPTION_JS, text)


def _wait_for(agent: str, *, active: bool, timeout: float) -> float:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        state = _get(f"{agent}/agent/status").get("workflow_state")
        if (state == "incident_active") == active:
            return time.monotonic() - start
        time.sleep(0.5)
    raise TimeoutError(f"workflow did not become {'active' if active else 'healthy'}")


def _wait_text(page: Page, selector: str, needle: str, timeout_ms: int) -> bool:
    """True once `selector` shows `needle`; False if the incident moved on first."""
    try:
        page.wait_for_function(
            "([sel, text]) => (document.querySelector(sel)?.innerText || '').includes(text)",
            arg=[selector, needle],
            timeout=timeout_ms,
        )
    except PlaywrightTimeoutError:
        return False
    return True


def _outage(page: Page, *, target: str, agent: str, fault: str, label: str, lead: str) -> float:
    page.click("#tab-live")
    _caption(page, lead)
    page.wait_for_timeout(3500)
    _caption(page, f"Simulated outage: {label}. MI is not told; it only sees symptoms.")
    started = time.monotonic()
    _post(f"{target}/ops/fault/{fault}")
    _wait_for(agent, active=True, timeout=30)
    _caption(page, "Health check failed: incident opened, evidence + SQLite docs (RAG) collected")
    if _wait_text(page, "#why", "failure_type", 60_000):
        _caption(page, "WHY: the LLM names the root cause and cites the evidence")
        page.wait_for_timeout(2500)
    _caption(page, "HOW: the LLM proposes repairs, JEV picks one, the security gate checks it")
    _wait_for(agent, active=False, timeout=240)
    took = time.monotonic() - started
    _caption(page, f"Recovered and verified in {took:.0f} s: one action, writes checked")
    page.wait_for_timeout(3000)
    page.click("#tab-history")
    page.wait_for_timeout(1500)
    page.click("button[data-history-incident]")
    page.wait_for_timeout(1500)
    _caption(page, "The full record: diagnosis, candidates, JEV scores, verdict, reward")
    page.wait_for_timeout(4000)
    for _ in range(8):
        page.mouse.wheel(0, 260)
        page.wait_for_timeout(1100)
    page.wait_for_timeout(2000)
    page.mouse.wheel(0, -20000)
    return took


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-url", default="http://127.0.0.1:8080")
    parser.add_argument("--agent-url", default="http://127.0.0.1:8090")
    parser.add_argument("--out", default="demo.mp4")
    parser.add_argument("--poster", default=None, help="optional PNG frame of the recovery")
    parser.add_argument("--channel", default="chrome", help="Playwright browser channel")
    args = parser.parse_args()
    target, agent = args.target_url.rstrip("/"), args.agent_url.rstrip("/")

    _wait_for(agent, active=False, timeout=120)
    with tempfile.TemporaryDirectory() as video_dir, sync_playwright() as pw:
        browser = pw.chromium.launch(channel=args.channel)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=video_dir,
            record_video_size={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.goto(f"{agent}/dashboard")
        page.wait_for_timeout(2500)
        _caption(page, "MI pings the service every second. Users keep writing data.")
        page.wait_for_timeout(5000)

        _outage(
            page,
            target=target,
            agent=agent,
            fault="corrupt_db",
            label="the database file is corrupted",
            lead="Outage 1",
        )
        if args.poster:
            _caption(page, "")
            page.mouse.wheel(0, -20000)
            page.wait_for_timeout(500)
            page.screenshot(path=args.poster)
        _outage(
            page,
            target=target,
            agent=agent,
            fault="drop_widgets_table",
            label="a table is dropped",
            lead="Outage 2: a different failure",
        )
        _caption(page, "Every incident, decision and verdict stays on the record")
        page.wait_for_timeout(4000)
        _caption(page, "")
        video_path = page.video.path() if page.video else None
        context.close()
        browser.close()
        if video_path is None:
            raise RuntimeError("no video recorded")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            shutil.copy(video_path, Path(args.out).with_suffix(".webm"))
            return
        subprocess.run(  # noqa: S603 - fixed ffmpeg argv
            [
                ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path),
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "26",
                "-movflags", "+faststart", args.out,
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
