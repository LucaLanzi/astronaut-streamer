#!/usr/bin/env python3
"""Run the whole pipeline on one machine and check it like a ground station would.

Starts the real MediaMTX (pinned release, rendered from jetson/mediamtx.yml.in),
the dashboard (jetson/app.py, HTTP + HTTPS) and the sender
(pi/sender.py --test-pattern), then opens the HTTPS dashboard in headless
Chrome and checks:

- WebRTC video and audio actually play;
- talk-back: Chrome's fake microphone is published from the page, the real
  pi/talkback.py receives it (recorded to a WAV instead of the headphones),
  and the recording has sound; mute and push-to-talk work;
- recovery: kills ffmpeg, restarts MediaMTX, stops the sender.

    make e2e          (sets up the environment first, then runs this)
    E2E_KEEP=1 make e2e   keep the logs and screenshot even when all checks pass

Uses ports 18000-19999 so it never collides with a real install. Needs ffmpeg
with libx264 + libopus (or imageio-ffmpeg), Flask, waitress, cheroot,
Playwright, openssl, and Google Chrome (or Playwright's Chromium).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
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

PORTS = {"WEB_PORT": "18080", "WEB_TLS_PORT": "18443", "RTSP_PORT": "18554",
         "WEBRTC_PORT": "18889", "WEBRTC_UDP_PORT": "18189", "HLS_PORT": "18888",
         "SRT_PORT": "18890", "MEDIAMTX_API_PORT": "19997"}
API = f"http://127.0.0.1:{PORTS['MEDIAMTX_API_PORT']}"
WEB = f"http://127.0.0.1:{PORTS['WEB_PORT']}"
WEB_TLS = f"https://127.0.0.1:{PORTS['WEB_TLS_PORT']}"
PUBLISH_URL = f"rtsp://127.0.0.1:{PORTS['RTSP_PORT']}/astronaut"
INSECURE = ssl._create_unverified_context()  # the dashboard's certificate is self-signed

work = Path(tempfile.mkdtemp(prefix="astronaut-e2e-"))
procs: dict[str, subprocess.Popen] = {}
results: list[tuple[bool, str]] = []
ENV: dict = {}


def check(name: str, ok, detail: str = "") -> bool:
    results.append((bool(ok), name))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return bool(ok)


def get(url: str, timeout: float = 2) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=INSECURE) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
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


def ffmpeg_path() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def environment() -> dict:
    env = {**os.environ, **PORTS, "JETSON_HOTSPOT_IP": "127.0.0.1", "STREAM_PATH": "astronaut",
           "TALKBACK_PATH": "talkback", "ASTRO_TLS_DIR": str(work / "tls")}
    if not shutil.which("ffmpeg"):
        d = work / "bin"
        d.mkdir()
        (d / "ffmpeg").symlink_to(ffmpeg_path())
        env["PATH"] = f"{d}:{env['PATH']}"
    return env


def make_certificate() -> bool:
    """A self-signed certificate like the installer makes."""
    (work / "tls").mkdir()
    out = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=astronaut-streamer e2e", "-addext", "subjectAltName=IP:127.0.0.1",
         "-keyout", str(work / "tls/dashboard.key"), "-out", str(work / "tls/dashboard.crt")],
        capture_output=True, text=True)
    return check("create a self-signed HTTPS certificate", out.returncode == 0,
                 out.stderr.strip()[-200:] if out.returncode else "")


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


def talkback_state() -> dict:
    code, body = get(f"{WEB}/api/talkback")
    return json.loads(body) if code == 200 else {}


def lan_ip() -> str | None:
    """This machine's LAN address: an http:// page there is NOT a secure context."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None


def peak_db(wav: Path) -> float | None:
    out = subprocess.run([ffmpeg_path(), "-hide_banner", "-nostats", "-i", str(wav),
                          "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"max_volume:\s*(-?inf|-?[\d.]+) dB", out.stderr)
    return float(m.group(1)) if m else None


def launch_browser(p):
    args = ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"]
    try:
        return p.chromium.launch(channel="chrome", headless=True, args=args)
    except Exception:
        try:
            return p.chromium.launch(headless=True, args=args)
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

TALK_JS = """() => {
  const pc = window.astro.talkPc;
  const sender = pc && pc.getSenders().find((s) => s.track && s.track.kind === 'audio');
  return {state: pc ? pc.connectionState : 'none', micOn: sender ? sender.track.enabled : null,
          status: document.getElementById('tb-status').textContent,
          mics: document.getElementById('tb-mic').options.length};
}"""


def talkback_checks(page) -> None:
    talk = lambda: page.evaluate(TALK_JS)  # noqa: E731
    check("talk-back: microphone dropdown and toggle are there", page.is_visible("#tb-mic")
          and "OFF" in page.inner_text("#tb-start"), page.inner_text("#tb-start"))
    wav = work / "talkback.wav"
    start("pi-talkback", [sys.executable, str(ROOT / "pi/talkback.py"), "--to-file", str(wav), "--once"])
    page.click("#tb-start")
    check("talk-back: the page publishes the mic (WHIP through the dashboard)",
          wait(lambda: talk()["state"] == "connected" and talkback_state().get("live"), 15),
          json.dumps(talk()))
    check("talk-back: the Pi's player picks it up",
          wait(lambda: talkback_state().get("listeners", 0) >= 1, 15), json.dumps(talkback_state()))
    check("talk-back: the page says the astronaut hears you",
          wait(lambda: "astronaut hears you" in talk()["status"], 10), talk()["status"])
    check("talk-back: the dropdown now lists the microphones by name",
          wait(lambda: talk()["mics"] > 1, 5), f"{talk()['mics']} entries")
    page.wait_for_timeout(3000)
    page.screenshot(path=str(work / "dashboard-talkback.png"))

    page.click("#tb-mute")
    check("talk-back: Mute turns the mic off", talk()["micOn"] is False, talk()["status"])
    page.keyboard.down("Space")
    ptt_on = talk()["micOn"]
    page.keyboard.up("Space")
    check("talk-back: holding Space talks while muted (push-to-talk)",
          ptt_on is True and talk()["micOn"] is False)
    page.click("#tb-mute")
    page.wait_for_timeout(1000)

    page.click("#tb-start")   # now "Stop talk-back"
    check("talk-back: Stop ends it", wait(lambda: not talkback_state().get("live"), 10))
    done = wait(lambda: procs["pi-talkback"].poll() is not None, 15)
    db = peak_db(wav) if wav.exists() else None
    check("talk-back: what the Pi received has sound in it", done and db is not None and db > -50,
          f"peak {db} dB" if db is not None else "no recording")
    stop("pi-talkback")


def browser_checks() -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = launch_browser(p)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto(WEB_TLS + "/")
        media = lambda: page.evaluate(MEDIA_JS)  # noqa: E731

        def playing(min_frames=30, after_connects=0):
            m = media()
            return m if (m["state"] == "connected" and m["frames"] > min_frames
                         and m["connects"] > after_connects) else None

        m = wait(playing, 25)
        check("browser (HTTPS page): WebRTC connects and decodes video", m, json.dumps(m or media()))
        if m:
            check("browser: video is 1280x720", m["width"] == 1280, f"width={m['width']}")
            check("browser: Opus audio arrives over WebRTC",
                  m["audioTracks"] == 1 and m["audioPackets"] > 20, f"packets={m['audioPackets']}")
            check("browser: starts muted, audio button showing", m["muted"] and m["unmuteVisible"])
            page.click("#unmute")
            m = media()
            check("browser: one click turns audio on", not m["muted"] and not m["unmuteVisible"])

        talkback_checks(page)
        page.screenshot(path=str(work / "dashboard.png"))

        before = media()["connects"]
        subprocess.run(["pkill", "-f", PUBLISH_URL])
        t0 = time.monotonic()
        back = wait(lambda: playing(after_connects=before), 30)
        check("recovery: ffmpeg killed -> stream and page come back on their own", back,
              f"{time.monotonic() - t0:.1f} s, reconnected" if back else "page didn't reconnect")

        before = media()["connects"]
        stop("mediamtx")
        check("page shows SERVER DOWN while MediaMTX is gone",
              wait(lambda: "SERVER DOWN" in media()["badge"] or None, 10))
        start("mediamtx", [str(mediamtx_binary()), str(work / "mediamtx.yml")])
        t0 = time.monotonic()
        back = wait(lambda: playing(after_connects=before), 45)
        check("recovery: MediaMTX restarted -> sender republishes, page reconnects", back,
              f"{time.monotonic() - t0:.1f} s")

        page.click("#btn-hls")
        page.wait_for_timeout(1000)
        src = page.evaluate("document.getElementById('hls').src")
        check("HLS button loads MediaMTX's player through the dashboard", src.endswith("/hls/astronaut/"), src)
        code = (wait(lambda: (lambda r: r if r[0] == 200 else None)(
            get(f"{WEB_TLS}/hls/astronaut/index.m3u8")), 20) or (0, ""))[0]
        check("HLS playlist is served (over HTTPS, through the dashboard)", code == 200, f"HTTP {code}")
        page.click("#btn-webrtc")
        check("switching back to WebRTC reconnects", wait(lambda: playing(10), 20))

        ip = lan_ip()
        if ip:
            plain = context.new_page()
            plain.goto(f"http://{ip}:{PORTS['WEB_PORT']}/")
            shown = wait(lambda: plain.evaluate("!document.getElementById('tb-insecure').hidden"), 5)
            check("plain HTTP page points to the secure page for talk-back", shown,
                  plain.evaluate("document.getElementById('tb-secure-link').href"))
            plain.close()

        stop("sender")
        check("sender stopped -> page says 'Waiting for astronaut feed'",
              wait(lambda: "Waiting" in media()["overlay"] or None, 15))
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
    if not (check("render the MediaMTX config from the template", render.returncode == 0,
                  render.stderr.strip()) and make_certificate()):
        return 1
    try:
        start("mediamtx", [str(mediamtx_binary()), str(work / "mediamtx.yml")])
        check("MediaMTX starts; its API answers on localhost",
              wait(lambda: get(f"{API}/v3/paths/list")[0] == 200, 10))
        code, body = path_state()
        check("path is not live before anything publishes",
              code == 404 or (code == 200 and not body.get("ready")), f"HTTP {code}")

        start("app", [sys.executable, str(ROOT / "jetson/app.py")])
        check("dashboard starts on HTTP", wait(lambda: get(f"{WEB}/healthz")[0] == 200, 10))
        check("dashboard starts on HTTPS", wait(lambda: get(f"{WEB_TLS}/healthz")[0] == 200, 10))
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
    if os.environ.get("E2E_KEEP"):
        print(f"\n{len(results)}/{len(results)} checks passed. Kept (E2E_KEEP): {work}")
        return 0
    shutil.rmtree(work, ignore_errors=True)
    print(f"\n{len(results)}/{len(results)} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
