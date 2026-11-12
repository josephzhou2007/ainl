"""Render video.html into the onboarding mp4 (1080x1920, 30 fps, H.264).

    .venv/bin/python scale-up/onboarding_video/render.py [out.mp4] [--fps 30] [--at 12.5 ...]

Each frame is the page seeked to an exact time and screenshotted, piped into
ffmpeg — no screen recording, so the result is the same on every run. --at
writes single PNG stills instead (for checking a moment without a full render).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(HERE / "onboarding.mp4"))
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--at", type=float, nargs="*", help="write stills at these seconds instead")
    a = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 540, "height": 960}, device_scale_factor=2)
        page.goto((HERE / "video.html").as_uri())
        page.evaluate("window.ready")
        duration = page.evaluate("window.DURATION")

        if a.at:
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            for t in a.at:
                page.evaluate("t => window.seek(t)", t)
                page.screenshot(path=str(out / f"t{t:05.1f}.png"))
            browser.close()
            return 0

        n = int(duration * a.fps)
        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(a.fps),
             "-c:v", "mjpeg", "-i", "-", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", a.out],
            stdin=subprocess.PIPE)
        for i in range(n):
            page.evaluate("t => window.seek(t)", i / a.fps)
            ff.stdin.write(page.screenshot(type="jpeg", quality=93))
            if i % (a.fps * 5) == 0:
                print(f"{i / a.fps:5.1f}s / {duration}s", file=sys.stderr, flush=True)
        ff.stdin.close()
        browser.close()
        return ff.wait()


if __name__ == "__main__":
    sys.exit(main())
