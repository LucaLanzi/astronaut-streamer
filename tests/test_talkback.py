"""Tests for pi/talkback.py: commands, output choice, and the start/stop loop
against a fake dashboard and a fake ffmpeg (no Pi, no network).

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pi"))
import devices as dv  # noqa: E402
import talkback  # noqa: E402

CFG = {"JETSON_HOTSPOT_IP": "10.42.0.1", "WEB_PORT": "8080", "RTSP_PORT": "8554",
       "TALKBACK_PATH": "talkback", "TALKBACK_MATCH": "Headphones", "TALKBACK_BUFFER_MS": "80"}


def card(index, card_id, name=None, playback=(0,), capture=()):
    return dv.SoundCard(index, card_id, "drv", name or card_id, "", tuple(capture), tuple(playback), None)


class CommandTests(unittest.TestCase):
    def test_decode_to_pcm_for_aplay(self):
        cmd = talkback.decode_cmd(CFG)
        self.assertEqual(cmd[cmd.index("-i") + 1], "rtsp://10.42.0.1:8554/talkback")
        self.assertIn("nobuffer", cmd)
        self.assertEqual(cmd[-3:], ["-f", "s16le", "-"])
        self.assertEqual(cmd[cmd.index("-ar") + 1], "48000")

    def test_decode_to_file(self):
        self.assertEqual(talkback.decode_cmd(CFG, to_file="/tmp/x.wav")[-3:], ["-f", "wav", "/tmp/x.wav"])

    def test_aplay_matches_the_pcm_format(self):
        cmd = talkback.play_cmd("plughw:CARD=Headphones,DEV=0", 80)
        self.assertEqual(cmd[cmd.index("-D") + 1], "plughw:CARD=Headphones,DEV=0")
        self.assertEqual(cmd[cmd.index("-f") + 1], "S16_LE")
        self.assertEqual(cmd[cmd.index("-c") + 1], "2")
        self.assertEqual(cmd[cmd.index("-r") + 1], "48000")
        self.assertEqual(cmd[cmd.index("-B") + 1], "80000", "buffer in microseconds")

    def test_api_url(self):
        self.assertEqual(talkback.jetson_url(CFG, "api"), "http://10.42.0.1:8080/api/talkback")


class OutputTests(unittest.TestCase):
    cards = [card(0, "vc4hdmi0"), card(1, "C920", capture=(0,), playback=()),
             card(2, "Headphones", "bcm2835 Headphones"), card(3, "UACDemoV10")]

    def test_headphones_jack_by_default(self):
        with mock.patch.object(dv, "list_sound_cards", return_value=self.cards):
            self.assertEqual(talkback.output_device(CFG), ("plughw:CARD=Headphones,DEV=0", "Headphones"))

    def test_usb_speaker_by_name(self):
        with mock.patch.object(dv, "list_sound_cards", return_value=self.cards):
            device, _ = talkback.output_device({**CFG, "TALKBACK_MATCH": "uacdemo"})
        self.assertEqual(device, "plughw:CARD=UACDemoV10,DEV=0")

    def test_missing(self):
        with mock.patch.object(dv, "list_sound_cards", return_value=self.cards[:2]):
            device, why = talkback.output_device(CFG)
        self.assertIsNone(device)
        self.assertIn("TALKBACK_MATCH=Headphones", why)


FAKE_FFMPEG = """#!/usr/bin/env python3
# ffmpeg stand-in: record that it ran, then "play" until interrupted.
import os, signal, sys, time
open(os.environ["FAKE_LOG"], "a").write("started\\n")
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
while True:
    time.sleep(0.05)
"""


class LoopTests(unittest.TestCase):
    """run() against a fake /api/talkback and a fake ffmpeg (to_file mode)."""

    def setUp(self):
        self.live = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"live": outer.live, "listeners": 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.ffmpeg = Path(self.tmp.name, "ffmpeg")
        self.ffmpeg.write_text(FAKE_FFMPEG)
        self.ffmpeg.chmod(0o755)
        self.log = Path(self.tmp.name, "calls.log")
        self.cfg = {**CFG, "JETSON_HOTSPOT_IP": "127.0.0.1", "WEB_PORT": str(self.server.server_port)}
        patches = [mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log)}),
                   mock.patch.object(talkback, "POLL_S", 0.1),
                   mock.patch.object(talkback, "OFFLINE_GRACE_S", 0.3)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def starts(self):
        return self.log.read_text().count("started") if self.log.exists() else 0

    def wait_for(self, predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_plays_while_live_and_stops_after(self):
        stop = threading.Event()
        result = {}
        thread = threading.Thread(target=lambda: result.update(code=talkback.run(
            self.cfg, to_file=str(Path(self.tmp.name, "out.wav")), once=True, stop=stop,
            ffmpeg=str(self.ffmpeg))), daemon=True)
        thread.start()
        time.sleep(0.4)
        self.assertEqual(self.starts(), 0, "nothing plays while nobody talks")
        self.live = True
        self.assertTrue(self.wait_for(lambda: self.starts() == 1), "starts when talk-back goes live")
        self.live = False
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "stops after the grace period (and --once exits)")
        self.assertEqual(result.get("code"), 0)
        self.assertEqual(self.starts(), 1)

    def test_dashboard_down_is_not_fatal(self):
        stop = threading.Event()
        cfg = {**self.cfg, "WEB_PORT": "1"}
        thread = threading.Thread(target=talkback.run, args=(cfg, str(Path(self.tmp.name, "o.wav"))),
                                  kwargs={"stop": stop, "ffmpeg": str(self.ffmpeg)}, daemon=True)
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "keeps retrying")
        self.assertEqual(self.starts(), 0)
        stop.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
