#!/usr/bin/env python3
"""Play the ground station's talk-back on the Pi's headphones (PLAN.md §3.6).

The ground station's dashboard publishes its microphone to MediaMTX on the
Jetson (stream TALKBACK_PATH). This asks the Jetson's dashboard once a second
whether that stream is live, and while it is, plays it on the Pi's 3.5 mm
jack (TALKBACK_MATCH, default "Headphones"):

    ffmpeg (RTSP in, decoded to raw PCM) | aplay (small buffer, low delay)

    ./talkback.py                 # what the astronaut-talkback service runs
    ./talkback.py --beep          # play a 1 s tone on the talk-back output
    ./talkback.py --to-file x.wav --once   # record one session instead (testing)

Standard library only (Python 3.10+). Settings: config/astronaut.env.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devices as dv  # noqa: E402
from envconfig import get_int  # noqa: E402  (path set up by devices)

log = logging.getLogger("talkback")

POLL_S = 1.0
OFFLINE_GRACE_S = 4.0     # stream reported gone this long while playing: stop
VOLUME_CONTROLS = ("PCM", "Headphone", "Master", "Speaker")


def jetson_url(cfg: dict, what: str) -> str:
    host = cfg.get("JETSON_HOTSPOT_IP") or "10.42.0.1"
    if what == "api":
        return f"http://{host}:{get_int(cfg, 'WEB_PORT', 8080)}/api/talkback"
    return f"rtsp://{host}:{get_int(cfg, 'RTSP_PORT', 8554)}/{cfg.get('TALKBACK_PATH') or 'talkback'}"


def decode_cmd(cfg: dict, ffmpeg: str = "ffmpeg", to_file: str | None = None) -> list[str]:
    """ffmpeg: pull the talk-back stream and decode it, to raw PCM on stdout
    (for aplay) or to a WAV file."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning",
           "-fflags", "nobuffer", "-flags", "low_delay", "-rtsp_transport", "tcp",
           "-i", jetson_url(cfg, "rtsp"), "-vn", "-ac", "2", "-ar", "48000"]
    if to_file:
        return cmd + ["-y", "-f", "wav", to_file]
    return cmd + ["-f", "s16le", "-"]


def play_cmd(device: str, buffer_ms: int) -> list[str]:
    """aplay: raw 48 kHz stereo PCM from stdin, with a small output buffer."""
    return ["aplay", "-q", "-D", device, "-t", "raw", "-f", "S16_LE", "-c", "2", "-r", "48000",
            "-B", str(buffer_ms * 1000)]


def talkback_live(cfg: dict, timeout: float = 1.5) -> bool | None:
    """True/False from the Jetson's dashboard; None if it can't be reached."""
    try:
        with urllib.request.urlopen(jetson_url(cfg, "api"), timeout=timeout) as resp:
            return bool(json.loads(resp.read()).get("live"))
    except (OSError, ValueError):
        return None


def output_device(cfg: dict) -> tuple[str | None, str]:
    """(ALSA device, card id) for talk-back, or (None, reason)."""
    match = cfg.get("TALKBACK_MATCH") or "Headphones"
    card = dv.pick_playback(dv.list_sound_cards(), match)
    if card is None:
        return None, f"no playback device matches TALKBACK_MATCH={match}"
    return card.alsa_device("playback"), card.id


def set_volume(card_id: str, volume: str) -> None:
    """Best effort: the first mixer control that takes the volume."""
    for control in VOLUME_CONTROLS:
        out = dv.run(["amixer", "-q", "-c", card_id, "sset", control, volume, "unmute"])
        if out.returncode == 0:
            log.info("volume: %s %s = %s", card_id, control, volume)
            return
    log.warning("couldn't set the volume on %s (no %s control); check alsamixer -c %s",
                card_id, "/".join(VOLUME_CONTROLS), card_id)


class Player:
    """One talk-back session: ffmpeg | aplay (or ffmpeg -> WAV)."""

    def __init__(self, cfg: dict, device: str | None, to_file: str | None = None,
                 ffmpeg: str = "ffmpeg"):
        self.procs: list[subprocess.Popen] = []
        decode = decode_cmd(cfg, ffmpeg, to_file)
        log.info("playing talk-back: %s", shlex.join(decode) if to_file else device)
        if to_file:
            self.procs.append(subprocess.Popen(decode, stderr=subprocess.PIPE, text=True))
        else:
            ff = subprocess.Popen(decode, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            ap = subprocess.Popen(play_cmd(device, get_int(cfg, "TALKBACK_BUFFER_MS", 80)),
                                  stdin=ff.stdout, stderr=subprocess.PIPE)
            ff.stdout.close()   # aplay owns the pipe now: it ends when ffmpeg does
            self.procs = [ff, ap]
        self.errors: list[str] = []
        for proc in self.procs:
            threading.Thread(target=self._collect, args=(proc,), daemon=True).start()

    def _collect(self, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            line = line.decode(errors="replace") if isinstance(line, bytes) else line
            if line.strip():
                self.errors.append(line.strip())
                if len(self.errors) <= 10:
                    log.warning("%s", line.strip())

    def running(self) -> bool:
        return all(p.poll() is None for p in self.procs)

    def stop(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        for proc in self.procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def run(cfg: dict, to_file: str | None = None, once: bool = False,
        stop: threading.Event | None = None, ffmpeg: str = "ffmpeg") -> int:
    stop = stop or threading.Event()
    device = None
    if not to_file:
        device, card = output_device(cfg)
        if device is None:
            log.error("%s; run make probe to see the Pi's sound devices", card)
            return 1
        set_volume(card, cfg.get("TALKBACK_VOLUME") or "90%")
    log.info("waiting for talk-back from the ground station (%s)", jetson_url(cfg, "api"))
    player: Player | None = None
    offline_since: float | None = None
    reachable = True
    while not stop.wait(POLL_S):
        live = talkback_live(cfg)
        if live is None and reachable:
            log.info("Jetson dashboard not reachable; retrying")
        reachable = live is not None
        if player and not player.running():
            log.info("talk-back ended")
            player.stop()
            player = None
            if once:
                return 0
        if live and player is None:
            player = Player(cfg, device, to_file, ffmpeg)
            offline_since = None
        elif not live and player is not None:
            offline_since = offline_since or time.monotonic()
            if time.monotonic() - offline_since > OFFLINE_GRACE_S:
                log.info("talk-back stopped at the ground station")
                player.stop()
                player = None
                if once:
                    return 0
    if player:
        player.stop()
    return 0


def beep(cfg: dict) -> int:
    device, card = output_device(cfg)
    if device is None:
        print(f"error: {card}", file=sys.stderr)
        return 1
    set_volume(card, cfg.get("TALKBACK_VOLUME") or "90%")
    ok, error = dv.beep(device)
    print(f"Played a 1 s tone on {device}" if ok else f"error: {error}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--beep", action="store_true", help="play a 1 s tone on the talk-back output")
    parser.add_argument("--to-file", metavar="WAV", help="record talk-back to a WAV file instead")
    parser.add_argument("--once", action="store_true", help="exit after one talk-back session")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
                        if sys.stderr.isatty() else "%(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = dv.load_config()
    if args.beep:
        return beep(cfg)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    return run(cfg, args.to_file, args.once, stop)


if __name__ == "__main__":
    sys.exit(main())
