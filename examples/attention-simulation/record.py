#!/usr/bin/env python3
"""Record a run of the day as one self-contained page: the real dashboard,
screenshotted after every beat by a headless Chromium, with the deck's clock,
timeline, narration and system state beside it — so the day can be watched
where nothing can run (a shared page), and the live run stays for those who
can run it.

    python3 examples/attention-simulation/record.py [--out dist/replay.html]
        [--chromium /path/to/headless_shell]

The output embeds every screenshot, so it is a few megabytes and needs
nothing else.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import simulate as S  # noqa: E402
import story  # noqa: E402

CANDIDATES = [
    os.environ.get("CHROMIUM", ""),
    "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell",
    "chromium", "chromium-browser", "google-chrome", "chrome",
]


def find_chromium(given: str | None) -> str:
    for c in ([given] if given else []) + CANDIDATES:
        if not c:
            continue
        path = shutil.which(c) or (c if Path(c).exists() else None)
        if path:
            return path
    sys.exit("record: no Chromium found — pass --chromium /path/to/chrome or headless_shell")


def shoot(chromium: str, url: str, out: Path, size: str = "420,900", budget: int = 5000) -> bool:
    cmd = [chromium, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
           f"--virtual-time-budget={budget}", f"--window-size={size}", f"--screenshot={out}", url]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
    except subprocess.TimeoutExpired:
        return False
    return out.exists() and out.stat().st_size > 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Record the day as a self-contained replay page.")
    ap.add_argument("--out", default=str(HERE / "dist" / "replay.html"))
    ap.add_argument("--chromium", default=None)
    ap.add_argument("--port", type=int, default=8768)
    args = ap.parse_args()
    chromium = find_chromium(args.chromium)
    shots_dir = Path(tempfile.mkdtemp(prefix="attention-record-"))
    tmp = Path(tempfile.mkdtemp(prefix="attention-simulation-"))
    sim = S.Simulation(tmp, args.port)
    sim.boot()
    sim.start()
    base = f"http://127.0.0.1:{args.port}"
    frames = []
    images: dict[str, str] = {}

    def capture(name: str, path: str) -> str | None:
        out = shots_dir / f"{name}.png"
        if shoot(chromium, base + path, out):
            images[name] = "data:image/png;base64," + base64.b64encode(out.read_bytes()).decode("ascii")
            return name
        return None

    t0 = time.time()
    # Midnight, before any beat: the list as the day begins.
    frames.append({"at": 0, "home": capture("f0-home", "/"), "view": None, "snapshot": sim.snapshot(), "feed_len": 0})
    for i, beat in enumerate(story.SCRIPT):
        sim.step()
        snap = sim.snapshot()
        name = f"f{i + 1}"
        home = capture(f"{name}-home", "/")
        view = None
        if sim.last_view and sim.last_view != "/" and sim.beats_done and sim.beats_done[-1].get("view") == sim.last_view:
            view = capture(f"{name}-view", sim.last_view)
        frames.append({"at": beat["at"], "home": home, "view": view, "view_url": sim.last_view if view else None,
                       "snapshot": snap, "feed_len": len(sim.feed)})
        print(f"[record] {S.Clock.hhmm(beat['at'])} {beat['who']:8s} {'view ' if view else ''}({time.time() - t0:.0f}s)", flush=True)
    final = sim.snapshot()
    data = {
        "date": final["date"],
        "beats": [{"at": b["at"], "who": b["who"], "text": b["text"], "action": bool(b.get("action")), "summary": bool(b.get("summary"))} for b in story.SCRIPT],
        "feed": final["feed"],
        "frames": [{k: v for k, v in f.items() if k != "snapshot"} | {"state": _state(f["snapshot"])} for f in frames],
        "schedule": final["attention"]["schedule"], "digest_times": final["attention"]["digest_times"],
        "modes": final["attention"]["modes"],
    }
    template = (HERE / "replay.template.html").read_text(encoding="utf-8")
    html = (template.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__IMAGES__", json.dumps(images).replace("</", "<\\/")))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[record] wrote {out} ({out.stat().st_size / 1e6:.1f} MB, {len(images)} screenshots, {time.time() - t0:.0f}s)", flush=True)
    shutil.rmtree(shots_dir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def _state(snap: dict) -> dict:
    att = snap.get("attention") or {}
    return {"time": snap["time"], "minute": snap["minute"], "mode": att.get("mode"), "next_breakpoint": att.get("next_breakpoint"),
            "counts": att.get("counts"), "learned": att.get("learned"), "stats": snap.get("stats"),
            "permits": att.get("permits"), "priors": att.get("priors"), "leads": att.get("leads")}


if __name__ == "__main__":
    sys.exit(main())
