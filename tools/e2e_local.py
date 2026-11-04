#!/usr/bin/env python3
"""Run the whole pipeline on one machine and check it like a ground station would.

Starts the real MediaMTX (pinned release, rendered from jetson/mediamtx.yml.in),
the dashboard (jetson/app.py) and the sender (pi/sender.py --test-pattern),
then opens the dashboard in headless Chrome and checks that WebRTC video and
audio actually play. Then it breaks things on purpose (kills ffmpeg, restarts
MediaMTX, stops the sender) and checks everything recovers on its own.

    make e2e          (sets up the environment first, then runs this)

Uses ports 18000-19999 so it never collides with a real install. Needs
ffmpeg with libx264 + libopus (or imageio-ffmpeg), Flask, waitress,
Playwright, and Google Chrome (or Playwright's Chromium).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
from envconfig import load_config  # noqa: E402

PORTS = {"WEB_PORT": "18080", "RTSP_PORT": "18554", "WEBRTC_PORT": "18889",
         "WEBRTC_UDP_PORT": "18189", "HLS_PORT": "18888", "SRT_PORT": "18890",
         "MEDIAMTX_API_PORT": "19997"}
API = f"http://127.0.0.1:{PORTS['MEDIAMTX_API_PORT']}"
WEB = f"http://127.0.0.1:{PORTS['WEB_PORT']}"
PUBLISH_URL = f"rtsp://127.0.0.1:{PORTS['RTSP_PORT']}/astronaut"

work = Path(tempfile.mkdtemp(prefix="astronaut-e2e-"))
procs: dict[str, subprocess.Popen] = {}
results: list[tuple[bool, str]] = []


def check(name: str, ok, detail: str = "") -> bool:
    results.append((bool(ok), name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return bool(ok)


def get(url: str, timeout: float = 2) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except OSError as e:
        return 0, str(e)


def wait(predicate, timeout: float, step: float = 0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return None


def mediamtx_binary() -> Path:
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}[platform.machine()]
    version = load_config().get("MEDIAMTX_VERSION", "v1.21.3")
    path = ROOT / "vendor" / f"mediamtx-{version}-{arch}" / "mediamtx"
    if not path.exists():
        sys.exit(f"{path} is missing: run `make e2e` (it runs tools/e2e_setup.sh first)")
    return path


def ffmpeg_dir() -> str:
    """A directory with an `ffmpeg` to put first on the sender's PATH."""
    if shutil.which("ffmpeg"):
        return ""
    import imageio_ffmpeg
    d = work / "bin"
    d.mkdir()
    (d / "ffmpeg").symlink_to(imageio_ffmpeg.get_ffmpeg_exe())
    return str(d)


def environment() -> dict:
    env = {**os.environ, **PORTS, "JETSON_HOTSPOT_IP": "127.0.0.1", "STREAM_PATH": "astronaut"}
    extra = ffmpeg_dir()
    if extra:
        env["PATH"] = f"{extra}:{env['PATH']}"
    return env


ENV: dict = {}


def start(name: str, cmd: list[str]) -> None:
    log = open(work / f"{name}.log", "w")
    # cwd=work: nothing a process writes to its working folder lands in the repo.
    procs[name] = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=ENV, cwd=work)


def stop(name: str) -> None:
    proc = procs.pop(name, None)
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def path_state() -> tuple[int, dict | str]:
    code, body = get(f"{API}/v3/paths/get/astronaut")
    try:
        return code, json.loads(body)
    except ValueError:
        return code, body


def launch_browser(p):
    try:
        return p.chromium.launch(channel="chrome", headless=True)
    except Exception:
        try:
            return p.chromium.launch(headless=True)
        except Exception as exc:
            sys.exit(f"no browser for Playwright ({exc}).\nInstall Google Chrome, or run: "
                     f"{Path(sys.executable).parent}/playwright install chromium")


MEDIA_JS = """async () => {
  const v = document.getElementById('video'), pc = window.astro.pc;
  const ov = document.getElementById('overlay');
  const out = {width: v.videoWidth, muted: v.muted, connects: window.astro.connects,
               audioTracks: v.srcObject ? v.srcObject.getAudioTracks().length : 0,
               state: pc ? pc.connectionState : 'none', frames: 0, audioPackets: 0,
               overlay: ov.hidden ? '' : ov.textContent,
               badge: document.getElementById('badge-feed').textContent,
               unmuteVisible: !document.getElementById('unmute').hidden};
  if (pc) (await pc.getStats()).forEach((s) => {
    if (s.type === 'inbound-rtp' && s.kind === 'video') out.frames = s.framesDecoded || 0;
    if (s.type === 'inbound-rtp' && s.kind === 'audio') out.audioPackets = s.packetsReceived || 0;
  });
  return out;
}"""


def browser_checks() -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(viewport={"width": 1400, "height": 860})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(WEB + "/")
        media = lambda: page.evaluate(MEDIA_JS)  # noqa: E731

        def playing(min_frames=30, after_connects=0):
            m = media()
            return m if (m["state"] == "connected" and m["frames"] > min_frames
                         and m["connects"] > after_connects) else None

        m = wait(playing, 25)
        check("browser: WebRTC connects and decodes video", m, json.dumps(m or media()))
        if m:
            check("browser: video is 1280x720", m["width"] == 1280, f"width={m['width']}")
            check("browser: Opus audio arrives over WebRTC",
                  m["audioTracks"] == 1 and m["audioPackets"] > 20, f"packets={m['audioPackets']}")
            check("browser: starts muted, audio button showing", m["muted"] and m["unmuteVisible"])
            page.click("#unmute")
            m = media()
            check("browser: one click turns audio on", not m["muted"] and not m["unmuteVisible"])
            page.screenshot(path=str(work / "dashboard.png"))

        before = media()["connects"]
        subprocess.run(["pkill", "-f", PUBLISH_URL])
        t0 = time.monotonic()
        back = wait(lambda: playing(after_connects=before), 30)
        check("recovery: ffmpeg killed -> stream and page come back on their own", back,
              f"{time.monotonic() - t0:.1f} s, reconnected" if back else "page didn't reconnect")

        before = media()["connects"]
        stop("mediamtx")
        down = wait(lambda: "SERVER DOWN" in media()["badge"] or None, 10)
        check("page shows SERVER DOWN while MediaMTX is gone", down)
        start("mediamtx", [str(mediamtx_binary()), str(work / "mediamtx.yml")])
        t0 = time.monotonic()
        back = wait(lambda: playing(after_connects=before), 45)
        check("recovery: MediaMTX restarted -> sender republishes, page reconnects", back,
              f"{time.monotonic() - t0:.1f} s")

        page.click("#btn-hls")
        page.wait_for_timeout(1000)
        src = page.evaluate("document.getElementById('hls').src")
        check("HLS button loads MediaMTX's HLS player", src.endswith(f":{PORTS['HLS_PORT']}/astronaut/"), src)
        code = (wait(lambda: (lambda r: r if r[0] == 200 else None)(
            get(f"http://127.0.0.1:{PORTS['HLS_PORT']}/astronaut/index.m3u8")), 20) or (0, ""))[0]
        check("HLS playlist is served", code == 200, f"HTTP {code}")
        page.click("#btn-webrtc")
        check("switching back to WebRTC reconnects", wait(lambda: playing(10), 20))

        stop("sender")
        waiting = wait(lambda: "Waiting" in media()["overlay"] or None, 15)
        check("sender stopped -> page says 'Waiting for astronaut feed'", waiting)
        check("no errors in the browser console", not errors, "; ".join(errors[:3]))
        browser.close()


def main() -> int:
    global ENV
    ENV = environment()
    print(f"Working folder (kept only if a check fails): {work}\n")
    render = subprocess.run(
        ["bash", "-c", f'. "{ROOT}/config/config.sh"; load_config; WEBRTC_IFACES=""; '
                       f'render_template "{ROOT}/jetson/mediamtx.yml.in" "{work}/mediamtx.yml"'],
        env=ENV, capture_output=True, text=True)
    if not check("render the MediaMTX config from the template", render.returncode == 0,
                 render.stderr.strip()):
        return 1
    try:
        start("mediamtx", [str(mediamtx_binary()), str(work / "mediamtx.yml")])
        check("MediaMTX starts; its API answers on localhost",
              wait(lambda: get(f"{API}/v3/paths/list")[0] == 200, 10))
        code, body = path_state()
        check("path is not live before anything publishes",
              code == 404 or (code == 200 and not body.get("ready")), f"HTTP {code}")

        start("app", [sys.executable, str(ROOT / "jetson/app.py")])
        check("dashboard starts", wait(lambda: get(f"{WEB}/healthz")[0] == 200, 10))
        feed = json.loads(get(f"{WEB}/api/status")[1])["feed"]
        check("dashboard says offline, media server up",
              not feed["live"] and feed["mediamtx"] == "up", feed["message"])

        start("sender", [sys.executable, str(ROOT / "pi/sender.py"), "--test-pattern"])
        live = wait(lambda: (lambda r: r if r[0] == 200 and isinstance(r[1], dict)
                             and r[1].get("ready") else None)(path_state()), 20)
        check("sender publishes; the path goes live", live)
        if live:
            tracks = [t if isinstance(t, str) else t.get("codec") for t in live[1].get("tracks", [])]
            check("tracks are H264 + Opus", {"H264", "Opus"} <= set(tracks), str(tracks))
        status = wait(lambda: (lambda s: s if s["pi"] and s["feed"].get("bitrate_kbps") else None)(
            json.loads(get(f"{WEB}/api/status")[1])), 15)
        check("dashboard shows bitrate and the sender's heartbeat", status,
              status and f"{status['feed']['bitrate_kbps']} kbit/s, pi {status['pi']['state']}")
        browser_checks()
    finally:
        for name in list(procs):
            stop(name)
    failed = [name for ok, name in results if not ok]
    if failed:
        print(f"\n{len(results) - len(failed)}/{len(results)} checks passed. "
              f"Logs and screenshot kept in {work}")
        return 1
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(results)}/{len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
