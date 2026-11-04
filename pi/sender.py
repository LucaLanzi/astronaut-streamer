#!/usr/bin/env python3
"""Stream the USB camera and its mic from the Pi to the Jetson (PLAN.md §3.2).

Runs ffmpeg (H.264 video + Opus audio, one muxed stream) pushing to MediaMTX
on the Jetson, and keeps it running:

- waits for the Jetson's hotspot and media server before starting;
- finds the camera and mic again after every restart (handles replugs);
- falls back from the Pi's hardware encoder to libx264 if it fails;
- sends video only when the mic is missing or fails, and adds audio back
  when a mic shows up again;
- restarts ffmpeg if it exits or stops making progress;
- posts a health heartbeat to the Jetson's dashboard every 2 s.

    ./sender.py                 # what the astronaut-sender service runs
    ./sender.py --test-pattern  # colour bars + 440 Hz tone instead of the camera
    ./sender.py --print-cmd     # show the ffmpeg command it would run, and exit
    ./sender.py --once          # one attempt, no restarts (debugging)

Standard library only (Python 3.10+). Settings: config/astronaut.env.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devices as dv  # noqa: E402
from envconfig import get_float, get_int  # noqa: E402  (path set up by devices)

log = logging.getLogger("sender")

HEARTBEAT_PERIOD_S = 2.0
STARTUP_GRACE_S = 20.0      # camera open + RTSP handshake before the first frame
STALL_S = 10.0              # no progress from ffmpeg for this long = stuck
MIC_RECHECK_S = 30.0        # while video-only, look for a mic this often
AUDIO_COOLDOWN_S = 60.0     # after the mic fails, stream video only this long
MAX_BACKOFF_S = 10.0
STDERR_LOG_LINES = 20       # ffmpeg messages logged per attempt; the rest only kept


# ------------------------------------------------------------ ffmpeg command

@dataclass
class Plan:
    """Everything one ffmpeg attempt needs."""
    encoder: str
    gop: bool = True
    camera: dv.Camera | None = None
    mode: dv.VideoMode | None = None
    mic: str | None = None          # ALSA device, None = video only
    test_pattern: bool = False

    @property
    def has_audio(self) -> bool:
        return self.test_pattern or self.mic is not None

    @property
    def video_text(self) -> str:
        if self.test_pattern:
            return "test pattern"
        return f"{self.camera.name if self.camera else '?'}, {self.mode}"

    def describe(self) -> str:
        video = self.video_text
        audio = "440 Hz tone" if self.test_pattern else (self.mic or "no audio")
        return f"{video} | {audio} | {self.encoder}{'' if self.gop else ' (no -g)'}"


def parse_bitrate(text: str) -> int:
    """'2M' -> 2_000_000, '800k' -> 800_000, '1500000' -> 1_500_000."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmM]?)\s*", text or "")
    if not m:
        raise ValueError(f"bad bitrate: {text!r}")
    scale = {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]
    return int(float(m.group(1)) * scale)


def stream_url(cfg: dict) -> str:
    host = cfg.get("JETSON_HOTSPOT_IP") or "10.42.0.1"
    path = cfg.get("STREAM_PATH") or "astronaut"
    if (cfg.get("TRANSPORT") or "rtsp").lower() == "srt":
        latency_us = get_int(cfg, "SRT_LATENCY_MS", 200) * 1000
        return (f"srt://{host}:{get_int(cfg, 'SRT_PORT', 8890)}"
                f"?streamid=publish:{path}&pkt_size=1316&latency={latency_us}")
    return f"rtsp://{host}:{get_int(cfg, 'RTSP_PORT', 8554)}/{path}"


def build_ffmpeg_cmd(cfg: dict, plan: Plan, ffmpeg: str = "ffmpeg") -> list[str]:
    """The ffmpeg command for one attempt (PLAN.md §3.2)."""
    if plan.test_pattern:
        width, height = get_int(cfg, "VIDEO_WIDTH", 1280), get_int(cfg, "VIDEO_HEIGHT", 720)
        fps = get_float(cfg, "VIDEO_FPS", 30)
    else:
        assert plan.camera and plan.mode, "a camera plan needs a camera and a mode"
        width, height, fps = plan.mode.width, plan.mode.height, plan.mode.fps
    bitrate = cfg.get("VIDEO_BITRATE") or "2M"

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning",
           "-progress", "pipe:1"]
    # Inputs: 0 = video, 1 = audio. Both are stamped with the wall clock so
    # they stay in sync.
    if plan.test_pattern:
        cmd += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps:g}",
                "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    else:
        cmd += ["-f", "v4l2", "-thread_queue_size", "512",
                "-input_format", dv.FFMPEG_INPUT_FORMAT.get(plan.mode.fourcc, "mjpeg"),
                "-video_size", f"{width}x{height}", "-framerate", f"{fps:g}",
                "-use_wallclock_as_timestamps", "1", "-i", plan.camera.path]
        if plan.mic:
            cmd += ["-f", "alsa", "-thread_queue_size", "1024", "-ac", "1", "-ar", "48000",
                    "-i", plan.mic]

    cmd += ["-map", "0:v:0"] + (["-map", "1:a:0"] if plan.has_audio else [])
    cmd += dv.encoder_args(plan.encoder, bitrate, fps, plan.gop)
    if plan.encoder == "libx264":
        cmd += ["-maxrate", bitrate, "-bufsize", f"{parse_bitrate(bitrate) // 2000}k"]
    if plan.has_audio:
        # Opus, because browsers' WebRTC only plays Opus and MediaMTX doesn't
        # convert. aresample=async=1 absorbs the mic clock drifting against
        # the camera's over long runs.
        cmd += ["-af", "aresample=async=1", "-c:a", "libopus",
                "-b:a", cfg.get("AUDIO_BITRATE") or "32k", "-ar", "48000", "-ac", "1",
                "-application", "voip"]
    else:
        cmd += ["-an"]

    url = stream_url(cfg)
    if url.startswith("srt://"):
        cmd += ["-f", "mpegts", url]
    else:
        cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", url]
    return cmd


def encoder_plan(cfg: dict, encoders: set[str], hw_node: str | None) -> list[tuple[str, bool]]:
    """Encoders to try, in order, as (encoder, use -g). `auto` tries the Pi's
    hardware encoder (with, then without a keyframe interval, since some
    kernels reject it) and falls back to libx264."""
    choice = (cfg.get("VIDEO_ENCODER") or "auto").strip()
    if choice == "libx264":
        return [("libx264", True)]
    if choice == "h264_v4l2m2m":
        return [("h264_v4l2m2m", True), ("h264_v4l2m2m", False)]
    plan = []
    if "h264_v4l2m2m" in encoders and hw_node:
        plan += [("h264_v4l2m2m", True), ("h264_v4l2m2m", False)]
    return plan + [("libx264", True)]


# --------------------------------------------------------- failure triage

# Only the encoder's own messages: camera errors also say "v4l2"
# ("[video4linux2,v4l2 @ ...]"), and must not cost us the hardware encoder.
ENCODER_ERROR_RE = re.compile(
    r"v4l2m2m|error (while opening|initializing) (encoder|output stream)", re.I)
# Only ALSA's messages: a busy *camera* also says "Device or resource busy".
AUDIO_ERROR_RE = re.compile(r"\[alsa @|cannot open audio device", re.I)


def is_encoder_failure(plan: Plan, frames: int, stderr: str) -> bool:
    """The hardware encoder failed (as opposed to the network or camera)."""
    return plan.encoder == "h264_v4l2m2m" and frames == 0 and bool(ENCODER_ERROR_RE.search(stderr))


def is_audio_failure(plan: Plan, stderr: str) -> bool:
    """The mic failed (busy, unplugged), so the next attempt should be video only."""
    return bool(plan.mic) and (f"{plan.mic}:" in stderr or bool(AUDIO_ERROR_RE.search(stderr)))


def parse_kbps(text: str) -> float | None:
    """ffmpeg's progress `bitrate=2001.3kbits/s` -> 2001.3."""
    m = re.match(r"\s*([\d.]+)\s*kbits/s", text or "")
    return float(m.group(1)) if m else None


def parse_speed(text: str) -> float | None:
    m = re.match(r"\s*([\d.]+)x", text or "")
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------ sender

@dataclass
class Attempt:
    frames: int = 0
    seconds: float = 0.0
    exit_code: int | None = None
    reason: str = ""                          # why we stopped it, if we did
    stderr: deque = field(default_factory=lambda: deque(maxlen=60))

    @property
    def stderr_text(self) -> str:
        return "\n".join(self.stderr)


class Sender:
    def __init__(self, cfg: dict, test_pattern: bool = False, heartbeat: bool = True,
                 ffmpeg: str = "ffmpeg"):
        self.cfg = cfg
        self.test_pattern = test_pattern
        self.heartbeat_enabled = heartbeat
        self.ffmpeg = ffmpeg
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.plan: Plan | None = None
        self.state = "starting"
        self.stats: dict = {}
        self.last_progress = 0.0
        self.restarts = 0
        self.last_error = ""
        self.started = time.monotonic()
        self.stream_started: float | None = None
        self.audio_retry_after = 0.0
        self.encoders = encoder_plan(cfg, dv.ffmpeg_encoders(ffmpeg), dv.hw_encoder_node())
        self.encoder_index = 0
        self._health_cache: dict = {}
        self._health_time = 0.0

    # --------------------------------------------------------------- plans

    def make_plan(self) -> Plan | None:
        """Find the devices and choose what this attempt streams. None means
        there's no usable camera right now."""
        encoder, gop = self.encoders[min(self.encoder_index, len(self.encoders) - 1)]
        if self.test_pattern:
            return Plan(encoder, gop, test_pattern=True)
        cameras = dv.list_cameras()
        camera = dv.pick_camera(cameras, self.cfg.get("CAMERA_MATCH", ""))
        mode = dv.pick_mode(camera.modes, get_int(self.cfg, "VIDEO_WIDTH", 1280),
                            get_int(self.cfg, "VIDEO_HEIGHT", 720),
                            get_float(self.cfg, "VIDEO_FPS", 30)) if camera else None
        if camera is None or mode is None:
            self.last_error = "no camera found" if camera is None else f"{camera.name}: no usable mode"
            return None
        mic = None
        if time.monotonic() >= self.audio_retry_after:
            card, why = dv.pick_mic(dv.list_sound_cards(), camera.usb_device,
                                    self.cfg.get("MIC_MATCH", ""))
            if card:
                mic = card.alsa_device("capture")
            else:
                log.warning("no mic (%s): streaming video only", why)
        return Plan(encoder, gop, camera, mode, mic)

    # ----------------------------------------------------------- main loop

    def run(self, once: bool = False) -> int:
        if self.heartbeat_enabled:
            threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        log.info("streaming to %s", stream_url(self.cfg))
        backoff = 1.0
        last_code = 0
        while not self.stop_event.is_set():
            if not self._wait_for_jetson():
                break
            plan = self.make_plan()
            if plan is None:
                self._set_state("no_camera")
                log.warning("%s; retrying in %.0f s", self.last_error, backoff)
                if self.stop_event.wait(backoff) or once:
                    return 1
                backoff = min(backoff * 2, MAX_BACKOFF_S)
                continue

            attempt = self._run_ffmpeg(plan)
            last_code = attempt.exit_code or 0
            if self.stop_event.is_set() or once:
                break
            self.restarts += 1
            stderr = attempt.stderr_text
            if attempt.reason == "add_audio":
                backoff = 1.0
                continue
            if is_encoder_failure(plan, attempt.frames, stderr) and \
                    self.encoder_index < len(self.encoders) - 1:
                self.encoder_index += 1
                nxt, gop = self.encoders[self.encoder_index]
                log.warning("%s failed to start; trying %s%s next", plan.encoder, nxt,
                            "" if gop else " without -g")
                continue
            if is_audio_failure(plan, stderr):
                self.audio_retry_after = time.monotonic() + AUDIO_COOLDOWN_S
                log.warning("mic failed; streaming video only for %.0f s", AUDIO_COOLDOWN_S)
                continue
            if attempt.seconds > 30:
                backoff = 1.0          # it was streaming fine: reconnect quickly
            log.warning("ffmpeg stopped after %.0f s (%s); restarting in %.0f s",
                        attempt.seconds, self.last_error or "no error", backoff)
            self._set_state("restarting")
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, MAX_BACKOFF_S)
        self._set_state("stopped")
        return last_code

    def stop(self, *_args) -> None:
        self.stop_event.set()

    def _set_state(self, state: str) -> None:
        with self.lock:
            self.state = state

    def _wait_for_jetson(self) -> bool:
        """Block until MediaMTX's RTSP port on the Jetson answers."""
        host = self.cfg.get("JETSON_HOTSPOT_IP") or "10.42.0.1"
        port = get_int(self.cfg, "RTSP_PORT", 8554)
        waited_since = None
        while not self.stop_event.is_set():
            try:
                with socket.create_connection((host, port), timeout=2):
                    if waited_since is not None:
                        log.info("Jetson is back after %.0f s", time.monotonic() - waited_since)
                    return True
            except OSError as exc:
                if waited_since is None:
                    waited_since = time.monotonic()
                    log.info("waiting for the Jetson at %s:%d (%s)", host, port, exc.strerror or exc)
                self._set_state("waiting_for_jetson")
                self.last_error = f"Jetson {host}:{port} unreachable"
                self.stop_event.wait(2)
        return False

    def _run_ffmpeg(self, plan: Plan) -> Attempt:
        cmd = build_ffmpeg_cmd(self.cfg, plan, self.ffmpeg)
        log.info("starting: %s", plan.describe())
        log.debug("ffmpeg command: %s", shlex.join(cmd))
        attempt = Attempt()
        start = time.monotonic()
        with self.lock:
            self.plan, self.stats, self.last_progress = plan, {}, start
            self.state = "starting"
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as exc:
            self.last_error = f"can't run ffmpeg: {exc}"
            attempt.exit_code = 127
            return attempt
        self.proc = proc
        readers = [threading.Thread(target=self._read_progress, args=(proc,), daemon=True),
                   threading.Thread(target=self._read_stderr, args=(proc, attempt), daemon=True)]
        for t in readers:
            t.start()

        next_mic_check = start + MIC_RECHECK_S
        while proc.poll() is None:
            now = time.monotonic()
            with self.lock:
                frames = int(self.stats.get("frame", "0") or 0)
                quiet = now - self.last_progress
            if frames and self.state != "streaming":
                self._set_state("streaming")
                self.stream_started = now
                log.info("streaming (%s)", plan.describe())
            if self.stop_event.is_set():
                attempt.reason = "stop"
            elif quiet > (STALL_S if frames else STARTUP_GRACE_S):
                attempt.reason = "stalled"
                self.last_error = f"ffmpeg stalled ({quiet:.0f} s without progress)"
                log.warning(self.last_error)
            elif (not plan.has_audio and frames and now >= next_mic_check
                  and now >= self.audio_retry_after):
                next_mic_check = now + MIC_RECHECK_S
                card, _ = dv.pick_mic(dv.list_sound_cards(),
                                      plan.camera.usb_device if plan.camera else None,
                                      self.cfg.get("MIC_MATCH", ""))
                if card:
                    attempt.reason = "add_audio"
                    log.info("mic found ([%s]); restarting to add audio", card.id)
            if attempt.reason:
                self._terminate(proc)
                break
            time.sleep(0.25)

        attempt.exit_code = proc.wait()
        for t in readers:
            t.join(timeout=2)
        attempt.seconds = time.monotonic() - start
        with self.lock:
            attempt.frames = int(self.stats.get("frame", "0") or 0)
            self.stream_started = None
        if attempt.exit_code != 0 and not attempt.reason:
            errors = [ln for ln in attempt.stderr if ln.strip()]
            self.last_error = errors[-1] if errors else f"ffmpeg exited with code {attempt.exit_code}"
        self.proc = None
        return attempt

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        """Ask ffmpeg to finish (SIGINT sends a clean RTSP TEARDOWN), then insist."""
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        except ProcessLookupError:
            pass

    def _read_progress(self, proc: subprocess.Popen) -> None:
        block: dict[str, str] = {}
        for line in proc.stdout:
            key, sep, value = line.strip().partition("=")
            if not sep:
                continue
            block[key] = value.strip()
            if key == "progress":
                with self.lock:
                    self.stats = block
                    self.last_progress = time.monotonic()
                block = {}

    def _read_stderr(self, proc: subprocess.Popen, attempt: Attempt) -> None:
        logged = 0
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            attempt.stderr.append(line)
            if logged < STDERR_LOG_LINES:
                log.warning("ffmpeg: %s", line)
                logged += 1
                if logged == STDERR_LOG_LINES:
                    log.warning("ffmpeg: (further messages this attempt are not logged)")

    # ------------------------------------------------------------ heartbeat

    def _health(self) -> dict:
        """Pi health, refreshed at most every 5 s (it runs iw and vcgencmd)."""
        now = time.monotonic()
        if now - self._health_time > 5:
            throttled = dv.get_throttled()
            self._health_cache = {
                "temp_c": dv.cpu_temp_c(),
                "throttled": f"{throttled:#x}" if throttled is not None else None,
                "throttle_flags": dv.decode_throttled(throttled) if throttled is not None else [],
                "wifi": dv.wifi_link(dv.wifi_iface(self.cfg.get("PI_WIFI_IFACE", ""))),
                "load": list(os.getloadavg()),
            }
            self._health_time = now
        return self._health_cache

    def heartbeat_payload(self) -> dict:
        with self.lock:
            plan, stats, state = self.plan, dict(self.stats), self.state
            streaming_for = (time.monotonic() - self.stream_started) if self.stream_started else 0
        payload = {
            "hostname": socket.gethostname(),
            "ip": local_ip_towards(self.cfg.get("JETSON_HOTSPOT_IP") or "10.42.0.1"),
            "state": state,
            "test_pattern": self.test_pattern,
            "video": plan.video_text if plan else None,
            "audio": bool(plan and plan.has_audio),
            "mic": plan.mic if plan else None,
            "encoder": plan.encoder if plan else None,
            "fps": float(stats["fps"]) if stats.get("fps") else None,
            "bitrate_kbps": parse_kbps(stats.get("bitrate", "")),
            "speed": parse_speed(stats.get("speed", "")),
            "dropped_frames": int(stats.get("drop_frames", "0") or 0),
            "streaming_s": round(streaming_for),
            "restarts": self.restarts,
            "uptime_s": round(time.monotonic() - self.started),
            "last_error": self.last_error,
        }
        payload.update(self._health())
        return payload

    def _heartbeat_loop(self) -> None:
        url = (f"http://{self.cfg.get('JETSON_HOTSPOT_IP') or '10.42.0.1'}:"
               f"{get_int(self.cfg, 'WEB_PORT', 8080)}/api/heartbeat")
        reachable = None
        while not self.stop_event.wait(HEARTBEAT_PERIOD_S):
            try:
                body = json.dumps(self.heartbeat_payload()).encode()
                req = urllib.request.Request(url, data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    resp.read()
                if reachable is not True:
                    log.info("dashboard heartbeat OK (%s)", url)
                reachable = True
            except Exception as exc:  # the dashboard being down must never stop the stream
                if reachable is not False:
                    log.info("dashboard not reachable for heartbeats (%s): %s", url, exc)
                reachable = False


def local_ip_towards(host: str) -> str:
    """This machine's IP on the route to `host` (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        return ""


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test-pattern", action="store_true",
                        help="send colour bars and a 440 Hz tone instead of the camera")
    parser.add_argument("--print-cmd", action="store_true",
                        help="print the ffmpeg command and exit")
    parser.add_argument("--once", action="store_true", help="one attempt, no restarts")
    parser.add_argument("--no-heartbeat", action="store_true", help="don't post heartbeats")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        # journald adds its own timestamps; a terminal needs them.
        format="%(asctime)s %(levelname)s %(message)s" if sys.stderr.isatty()
        else "%(levelname)s %(message)s",
        datefmt="%H:%M:%S")

    cfg = dv.load_config()
    sender = Sender(cfg, test_pattern=args.test_pattern, heartbeat=not args.no_heartbeat)
    if args.print_cmd:
        plan = sender.make_plan()
        if plan is None:
            print(f"error: {sender.last_error}", file=sys.stderr)
            return 1
        print(shlex.join(build_ffmpeg_cmd(cfg, plan)))
        return 0
    signal.signal(signal.SIGTERM, sender.stop)
    signal.signal(signal.SIGINT, sender.stop)
    return sender.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
