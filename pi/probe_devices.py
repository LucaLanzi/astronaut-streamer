#!/usr/bin/env python3
"""Phase 0 bench check for the astronaut Pi.

Lists the USB camera and its modes, finds the camera's mic and the USB
speaker, records a few seconds from the mic, benchmarks the H.264 encoders
(synthetic and on the live camera), and reports Pi health and the Wi-Fi link
to the Jetson. Exits non-zero if something the stream needs is missing.

    ./probe_devices.py              # full check (~30 s)
    ./probe_devices.py --no-encode  # skip the encoder benchmarks
    ./probe_devices.py --beep       # also play a tone on the USB speaker
    ./probe_devices.py --json       # machine-readable output

Settings (resolution, CAMERA_MATCH, MIC_MATCH, ...) come from
config/astronaut.env; any of them can be overridden from the environment,
e.g. `VIDEO_WIDTH=640 VIDEO_HEIGHT=480 ./probe_devices.py`.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import platform
import re
import shutil
import sys
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devices as dv  # noqa: E402

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
TAGS = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[ -- ]"}


class Report:
    """Prints each check as it runs and keeps them for the summary / JSON."""

    def __init__(self, quiet: bool):
        self.quiet = quiet
        self.items: list[dict] = []
        self.data: dict = {}
        self.section = ""

    def heading(self, title: str) -> None:
        self.section = title
        if not self.quiet:
            print(f"\n== {title} ==", flush=True)

    def add(self, status: str, text: str, detail: str = "") -> None:
        self.items.append({"status": status, "section": self.section,
                           "text": text, "detail": detail})
        if not self.quiet:
            print(f"{TAGS[status]} {text}")
            for line in detail.strip().splitlines():
                print(f"       {line}")
            sys.stdout.flush()


def wanted_mode(cfg: dict) -> tuple[int, int, float]:
    return (int(cfg.get("VIDEO_WIDTH") or 1280), int(cfg.get("VIDEO_HEIGHT") or 720),
            float(cfg.get("VIDEO_FPS") or 30))


def summarize_modes(modes: list[dv.VideoMode]) -> str:
    """One line per pixel format: every size with its highest frame rate."""
    by_format: dict[str, dict[tuple[int, int], float]] = {}
    for m in modes:
        sizes = by_format.setdefault(m.fourcc, {})
        sizes[(m.width, m.height)] = max(sizes.get((m.width, m.height), 0), m.fps)
    lines = []
    for fourcc, sizes in by_format.items():
        ordered = sorted(sizes.items(), key=lambda kv: -kv[0][0] * kv[0][1])
        lines.append(f"{fourcc}: " + ", ".join(f"{w}x{h}@{fps:g}" for (w, h), fps in ordered))
    return "\n".join(lines) or "(no modes reported — is v4l2-ctl installed?)"


# ---------------------------------------------------------------------- checks

def check_system(r: Report) -> None:
    r.heading("System")
    model = dv.pi_model()
    r.add(OK if "Raspberry Pi 4" in model else WARN, f"Board: {model}")
    release = dv.parse_env(dv._read("/etc/os-release"))
    os_name = release.get("PRETTY_NAME", "unknown")
    arch = platform.machine()
    expected = release.get("VERSION_CODENAME") == "bookworm" and arch == "aarch64"
    r.add(OK if expected else WARN, f"OS: {os_name}, {arch}, kernel {platform.release()}",
          "" if expected else "expected Raspberry Pi OS Lite 64-bit (Debian 12 bookworm, aarch64)")

    temp = dv.cpu_temp_c()
    if temp is not None:
        r.add(OK if temp < 70 else WARN, f"CPU temperature: {temp:.1f} °C")
    value = dv.get_throttled()
    if value is None:
        r.add(INFO, "Throttle flags: vcgencmd not available (sudo apt install libraspberrypi-bin)")
    else:
        flags = dv.decode_throttled(value)
        r.add(WARN if flags else OK, f"Throttle flags: {value:#x}" + ("" if flags else " (none)"),
              "\n".join(flags))

    if os.geteuid() != 0:
        groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
        missing = sorted({"video", "audio"} - groups)
        if missing:
            r.add(WARN, f"User isn't in group(s): {', '.join(missing)}",
                  "fine at the desktop; over SSH run: sudo usermod -aG video,audio $USER\n"
                  "then log out and back in")


def check_tools(r: Report) -> set[str]:
    r.heading("Tools")
    version = dv.run(["ffmpeg", "-hide_banner", "-version"])
    if version.returncode != 0:
        r.add(FAIL, "ffmpeg isn't installed (sudo apt install ffmpeg)")
        return set()
    r.add(OK, version.stdout.splitlines()[0] if version.stdout else "ffmpeg")
    for tool, package, status in (("v4l2-ctl", "v4l-utils", FAIL), ("iw", "iw", WARN)):
        if shutil.which(tool) is None:
            r.add(status, f"{tool} isn't installed (sudo apt install {package})")

    encoders = dv.ffmpeg_encoders()
    for name, purpose, status in (("libx264", "software H.264", FAIL),
                                  ("libopus", "Opus audio", FAIL),
                                  ("h264_v4l2m2m", "Pi hardware H.264", WARN)):
        if name in encoders:
            r.add(OK, f"ffmpeg has {name} ({purpose})")
        else:
            r.add(status, f"ffmpeg is missing {name} ({purpose})")
    node = dv.hw_encoder_node()
    if node:
        r.add(OK, f"Hardware encoder node: {node}")
    else:
        r.add(WARN, "Hardware encoder node not found (bcm2835-codec not loaded): software encoding only")
    r.data["encoders"] = sorted(e for e in encoders if e in ("libx264", "libopus", "h264_v4l2m2m"))
    return encoders


def check_camera(r: Report, cfg: dict):
    r.heading("Camera")
    want = wanted_mode(cfg)
    cameras = dv.list_cameras()
    if not cameras:
        r.add(FAIL, "No USB camera found (check the cable, then `v4l2-ctl --list-devices`)")
        return None, None
    for cam in cameras:
        where = cam.path + (f" -> {cam.node}" if cam.node != cam.path else "")
        r.add(INFO, f"{cam.name}: {where}", summarize_modes(cam.modes))

    match = cfg.get("CAMERA_MATCH", "")
    cam = dv.pick_camera(cameras, match)
    if cam is None:
        r.add(FAIL, f"No camera matches CAMERA_MATCH={match}")
        return None, None
    r.data["camera"] = asdict(cam)
    if not os.access(cam.node, os.R_OK | os.W_OK):
        r.add(FAIL, f"No permission to open {cam.node}", "add your user to the video group")
    mode = dv.pick_mode(cam.modes, *want)
    if mode is None:
        r.add(FAIL, f"{cam.name} has no MJPG or YUYV mode")
        return cam, None
    exact = (mode.fourcc == "MJPG" and (mode.width, mode.height) == want[:2]
             and mode.fps + 0.5 >= want[2])
    asked = f"asked for MJPG {want[0]}x{want[1]} @ {want[2]:g} fps"
    r.add(OK if exact else WARN, f"Selected: {cam.name}, {mode}" + ("" if exact else f" ({asked})"))
    if any(m.fourcc == "H264" for m in cam.modes):
        r.add(INFO, "This camera can also output H.264 itself (could skip encoding on the Pi)")
    r.data["mode"] = asdict(mode)
    return cam, mode


def check_audio(r: Report, cfg: dict, cam, seconds: float, play_beep: bool) -> None:
    r.heading("Audio")
    cards = dv.list_sound_cards()
    if not cards:
        r.add(WARN, "No sound cards found: the stream would be video only")
        return
    for c in cards:
        kinds = " + ".join(k for k, has in (("capture", c.capture), ("playback", c.playback)) if has)
        r.add(INFO, f"card {c.index} [{c.id}] {c.name}: {kinds or 'no PCM devices'}"
                    + (", USB" if c.usb_device else ""))
    camera_usb = cam.usb_device if cam else None

    mic, why = dv.pick_mic(cards, camera_usb, cfg.get("MIC_MATCH", ""))
    if mic is None:
        r.add(WARN, f"Mic: none selected ({why})", "the stream would be video only")
    else:
        device = mic.alsa_device("capture")
        trusted = why == "same USB device as the camera" or why.startswith("MIC_MATCH")
        r.add(OK if trusted else WARN, f"Mic: [{mic.id}] {mic.name} -> {device} ({why})")
        r.data["mic"] = {**asdict(mic), "alsa": device}
        result = dv.check_mic(device, seconds)
        if not result.ok:
            r.add(WARN, f"Mic test failed: {result.error}",
                  "the stream would be video only; find what holds it: sudo fuser -v /dev/snd/*"
                  if "busy" in result.error else "the stream would be video only")
        elif result.max_db is not None and result.max_db < -60:
            r.add(WARN, f"Mic recorded {seconds:g} s but it's nearly silent (peak {result.max_db:g} dB)",
                  f"talk or clap during the test; check it isn't muted: alsamixer -c {mic.id}")
        else:
            r.add(OK, f"Mic recorded {seconds:g} s: peak {result.max_db:g} dB, mean {result.mean_db:g} dB")

    speaker, why = dv.pick_speaker(cards, camera_usb, cfg.get("SPEAKER_MATCH", ""))
    if speaker is None:
        r.add(INFO, f"Speaker: none ({why})")
        return
    device = speaker.alsa_device("playback")
    r.add(OK, f"Speaker: [{speaker.id}] {speaker.name} -> {device} ({why})")
    r.data["speaker"] = {**asdict(speaker), "alsa": device}
    if speaker.capture and mic is not None and speaker.id != mic.id:
        r.add(INFO, f"[{speaker.id}] also has a mic; the camera's mic is used (set MIC_MATCH to change)")
    if play_beep:
        ok, error = dv.beep(device)
        r.add(OK if ok else WARN,
              "Played a 1 s tone on the speaker: did you hear it?" if ok else f"Speaker test failed: {error}")


def check_encoders(r: Report, cfg: dict, encoders: set[str], cam, mode, seconds: float) -> None:
    r.heading("H.264 encoding")
    width, height, fps = wanted_mode(cfg)
    bitrate = cfg.get("VIDEO_BITRATE") or "2M"
    results = []
    for encoder in ("h264_v4l2m2m", "libx264"):
        if encoder not in encoders:
            continue
        if encoder == "h264_v4l2m2m" and dv.hw_encoder_node() is None:
            r.add(INFO, "h264_v4l2m2m: skipped (no hardware encoder node)")
            continue
        result = dv.benchmark_encoder(encoder, dv.synthetic_input(width, height, fps),
                                      f"test pattern {width}x{height}@{fps:g}",
                                      seconds, fps, bitrate)
        report_encode(r, result, live=False)
        results.append(result)
    working = [x for x in results if x.ok]
    r.data["encode"] = [asdict(x) for x in results]
    if not working:
        r.add(FAIL, "No H.264 encoder works")
        return
    if cam is None or mode is None:
        return
    # Use the hardware encoder on the live test if it has headroom (it frees
    # the CPU); otherwise whichever encoder was fastest.
    fast_hw = [x for x in working if x.encoder == "h264_v4l2m2m" and (x.speed or 0) >= 1.2]
    best = fast_hw[0] if fast_hw else max(working, key=lambda x: x.speed or 0)
    live = dv.benchmark_encoder(best.encoder, dv.camera_input(cam, mode), f"camera {mode}",
                                seconds, mode.fps, bitrate)
    report_encode(r, live, live=True)
    r.data["encode"].append(asdict(live))


def report_encode(r: Report, res: dv.EncodeResult, live: bool) -> None:
    if not res.ok:
        r.add(WARN, f"{res.encoder} on {res.source}: failed: {res.error}")
        return
    speed = f"{res.speed:.2f}x real time" if res.speed is not None else f"{res.fps:.1f} fps"
    text = f"{res.encoder} on {res.source}: {speed}, {res.cpu_cores:.1f} CPU cores"
    notes = [res.note] if res.note else []
    if live:
        keeps_up = res.speed is None or res.speed >= 0.9
        if not keeps_up:
            notes.append("can't keep up: lower VIDEO_WIDTH/VIDEO_HEIGHT or VIDEO_FPS")
        if res.cpu_cores >= 2.5:
            notes.append("uses most of the Pi's 4 cores: expect heat and throttling")
        status = OK if keeps_up and res.cpu_cores < 2.5 else WARN
    else:
        status = OK if (res.speed or 0) >= 1.2 else WARN
        if status == WARN:
            notes.append("little or no headroom at this resolution")
    r.add(status, text, "\n".join(notes))


def check_network(r: Report, cfg: dict) -> None:
    r.heading("Wi-Fi link to the Jetson")
    iface = cfg.get("PI_WIFI_IFACE") or "wlan0"
    ssid = cfg.get("HOTSPOT_SSID") or "ASTRO-LINK"
    link = dv.wifi_link(iface)
    r.data["wifi"] = link
    if not link:
        r.add(WARN, f"{iface} isn't connected to any Wi-Fi network",
              "once the Jetson's hotspot is up: sudo nmcli con up astro-link")
    else:
        on_hotspot = link.get("ssid") == ssid
        signal = link.get("signal_dbm")
        detail = (f"signal {signal if signal is not None else '?'} dBm, "
                  f"{link.get('freq_mhz', '?')} MHz, tx {link.get('tx_bitrate', '?')}")
        weak = signal is not None and signal < -75
        r.add(OK if on_hotspot and not weak else WARN,
              f"{iface} connected to {link.get('ssid')}" + ("" if on_hotspot else f" (expected {ssid})"),
              detail + ("\nweak signal: expect drops" if weak else ""))
    jetson = cfg.get("JETSON_HOTSPOT_IP") or "10.42.0.1"
    ping = dv.run(["ping", "-c", "3", "-W", "1", "-q", jetson], timeout=10)
    if ping.returncode == 0:
        m = re.search(r"= [\d.]+/([\d.]+)/", ping.stdout)
        r.add(OK, f"Jetson {jetson} reachable" + (f", average {m.group(1)} ms" if m else ""))
    else:
        r.add(WARN, f"Jetson {jetson} not reachable (expected until the hotspot is up and joined)")


# ------------------------------------------------------------------------ main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print JSON instead of the report")
    parser.add_argument("--no-encode", action="store_true", help="skip the encoder benchmarks")
    parser.add_argument("--beep", action="store_true", help="play a test tone on the USB speaker")
    parser.add_argument("--seconds", type=float, default=5,
                        help="length of each encoder benchmark (default 5)")
    args = parser.parse_args(argv)

    cfg = dv.load_config()
    r = Report(quiet=args.json)
    check_system(r)
    encoders = check_tools(r)
    cam, mode = check_camera(r, cfg)
    check_audio(r, cfg, cam, seconds=3, play_beep=args.beep)
    if not args.no_encode and encoders:
        check_encoders(r, cfg, encoders, cam, mode, args.seconds)
    check_network(r, cfg)

    fails = [i for i in r.items if i["status"] == FAIL]
    warns = [i for i in r.items if i["status"] == WARN]
    if args.json:
        print(json.dumps({"checks": r.items, **r.data}, indent=2, default=str))
    else:
        print(f"\n== Summary: {len(fails)} problem(s), {len(warns)} warning(s) ==")
        for item in fails + warns:
            print(f"{TAGS[item['status']]} {item['section']}: {item['text']}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
