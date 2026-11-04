"""Tests for pi/sender.py: the ffmpeg command, failure triage, and the
supervisor loop (against a fake ffmpeg, so no camera or network needed).

    python3 -m unittest discover -s tests -v
"""

import os
import socket
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pi"))
import devices as dv  # noqa: E402
import sender  # noqa: E402

CFG = {
    "JETSON_HOTSPOT_IP": "10.42.0.1", "RTSP_PORT": "8554", "SRT_PORT": "8890",
    "STREAM_PATH": "astronaut", "VIDEO_WIDTH": "1280", "VIDEO_HEIGHT": "720",
    "VIDEO_FPS": "30", "VIDEO_BITRATE": "2M", "AUDIO_BITRATE": "32k",
    "TRANSPORT": "rtsp", "SRT_LATENCY_MS": "200", "VIDEO_ENCODER": "auto", "WEB_PORT": "8080",
}
CAM = dv.Camera("/dev/v4l/by-id/usb-C920-video-index0", "/dev/video0", "C920", "usb1/1-1/1-1.2")
MODE = dv.VideoMode("MJPG", 1280, 720, 30.0)
MIC = "plughw:CARD=C920,DEV=0"


def after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class CommandTests(unittest.TestCase):
    def test_camera_mic_libx264_rtsp(self):
        cmd = sender.build_ffmpeg_cmd(CFG, sender.Plan("libx264", True, CAM, MODE, MIC))
        cam_i = cmd.index(CAM.path)
        self.assertEqual(cmd[cam_i - 1], "-i")
        self.assertLess(cmd.index("-use_wallclock_as_timestamps"), cam_i,
                        "wall-clock stamping is an input option of the camera")
        self.assertEqual(after(cmd, "-input_format"), "mjpeg")
        self.assertIn(MIC, cmd)
        self.assertGreater(cmd.index(MIC), cam_i, "audio is input 1")
        self.assertIn("1:a:0", cmd)
        self.assertEqual(after(cmd, "-c:a"), "libopus")
        self.assertEqual(after(cmd, "-ar"), "48000")
        self.assertEqual(after(cmd, "-maxrate"), "2M")
        self.assertEqual(after(cmd, "-bufsize"), "1000k")
        self.assertEqual(after(cmd, "-g"), "30")
        self.assertEqual(cmd[-3:], ["-rtsp_transport", "tcp", "rtsp://10.42.0.1:8554/astronaut"])
        self.assertIn("pipe:1", cmd, "progress goes to stdout for the supervisor")

    def test_video_only(self):
        cmd = sender.build_ffmpeg_cmd(CFG, sender.Plan("libx264", True, CAM, MODE, None))
        self.assertIn("-an", cmd)
        self.assertNotIn("alsa", cmd)
        self.assertNotIn("1:a:0", cmd)
        self.assertNotIn("libopus", cmd)

    def test_test_pattern(self):
        cmd = sender.build_ffmpeg_cmd(CFG, sender.Plan("libx264", test_pattern=True))
        self.assertEqual(cmd.count("lavfi"), 2)
        self.assertEqual(cmd.count("-re"), 2, "both test sources paced to real time")
        self.assertTrue(any(a.startswith("testsrc2=size=1280x720") for a in cmd))
        self.assertTrue(any(a.startswith("sine=") for a in cmd))
        self.assertIn("libopus", cmd)

    def test_hardware_encoder_without_gop(self):
        cmd = sender.build_ffmpeg_cmd(CFG, sender.Plan("h264_v4l2m2m", False, CAM, MODE, MIC))
        self.assertEqual(after(cmd, "-c:v"), "h264_v4l2m2m")
        self.assertNotIn("-g", cmd)
        self.assertNotIn("-maxrate", cmd, "rate control flags are for libx264")

    def test_srt(self):
        cfg = {**CFG, "TRANSPORT": "srt"}
        cmd = sender.build_ffmpeg_cmd(cfg, sender.Plan("libx264", True, CAM, MODE, MIC))
        self.assertEqual(cmd[-3:-1], ["-f", "mpegts"])
        self.assertEqual(cmd[-1], "srt://10.42.0.1:8890?streamid=publish:astronaut"
                                  "&pkt_size=1316&latency=200000")

    def test_parsers(self):
        self.assertEqual(sender.parse_bitrate("2M"), 2_000_000)
        self.assertEqual(sender.parse_bitrate("800k"), 800_000)
        self.assertEqual(sender.parse_bitrate("1.5M"), 1_500_000)
        with self.assertRaises(ValueError):
            sender.parse_bitrate("fast")
        self.assertEqual(sender.parse_kbps("2001.3kbits/s"), 2001.3)
        self.assertIsNone(sender.parse_kbps("N/A"))
        self.assertEqual(sender.parse_speed("1.01x"), 1.01)
        self.assertIsNone(sender.parse_speed("N/A"))


class EncoderPlanTests(unittest.TestCase):
    def test_auto_with_hardware(self):
        plan = sender.encoder_plan(CFG, {"h264_v4l2m2m", "libx264"}, "/dev/video11")
        self.assertEqual(plan, [("h264_v4l2m2m", True), ("h264_v4l2m2m", False), ("libx264", True)])

    def test_auto_without_hardware(self):
        self.assertEqual(sender.encoder_plan(CFG, {"h264_v4l2m2m", "libx264"}, None),
                         [("libx264", True)])

    def test_forced(self):
        self.assertEqual(sender.encoder_plan({**CFG, "VIDEO_ENCODER": "libx264"}, set(), "/x"),
                         [("libx264", True)])


class TriageTests(unittest.TestCase):
    hw = sender.Plan("h264_v4l2m2m", True, CAM, MODE, MIC)

    def test_encoder_failure(self):
        err = "[h264_v4l2m2m @ 0x55] Could not find a valid device"
        self.assertTrue(sender.is_encoder_failure(self.hw, 0, err))
        self.assertFalse(sender.is_encoder_failure(self.hw, 120, err), "it streamed: not the encoder")
        self.assertFalse(sender.is_encoder_failure(
            sender.Plan("libx264", True, CAM, MODE, MIC), 0, err))

    def test_camera_error_is_not_an_encoder_failure(self):
        err = "[video4linux2,v4l2 @ 0x55] Cannot open video device /dev/video0: Device or resource busy"
        self.assertFalse(sender.is_encoder_failure(self.hw, 0, err))
        self.assertFalse(sender.is_audio_failure(self.hw, err), "a busy camera isn't the mic")

    def test_audio_failure(self):
        err = f"[alsa @ 0x55] cannot open audio device {MIC} (Device or resource busy)"
        self.assertTrue(sender.is_audio_failure(self.hw, err))
        self.assertTrue(sender.is_audio_failure(self.hw, f"{MIC}: Input/output error"))
        self.assertFalse(sender.is_audio_failure(sender.Plan("libx264", True, CAM, MODE, None), err))


FAKE_FFMPEG = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stand-in for ffmpeg: the "hardware encoder" always fails; with "stall"
    # in FAKE_MODE it goes quiet after a few frames; otherwise it reports
    # progress until interrupted.
    import os, signal, sys, time
    args = sys.argv[1:]
    if "-encoders" in args:
        print(" V..... h264_v4l2m2m  V4L2 mem2mem H.264 encoder wrapper")
        print(" V....D libx264       libx264 H.264")
        sys.exit(0)
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(" ".join(args) + "\\n")
    if "h264_v4l2m2m" in args:
        print("[h264_v4l2m2m @ 0x1] Could not find a valid device", file=sys.stderr)
        sys.exit(1)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(255))
    frame = 0
    while True:
        frame += 3
        print(f"frame={frame}\\nfps=30.0\\nbitrate=2000.0kbits/s\\nspeed=1.00x\\nprogress=continue",
              flush=True)
        time.sleep(0.1)
        if os.environ.get("FAKE_MODE") == "stall" and frame > 6:
            time.sleep(3600)
""")


class SupervisorTests(unittest.TestCase):
    """Sender.run() against the fake ffmpeg and a local stand-in for MediaMTX."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.ffmpeg = tmp / "ffmpeg"
        self.ffmpeg.write_text(FAKE_FFMPEG)
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IEXEC)
        self.log = tmp / "calls.log"
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.accepting = True
        threading.Thread(target=self._accept, daemon=True).start()
        self.cfg = {**CFG, "JETSON_HOTSPOT_IP": "127.0.0.1",
                    "RTSP_PORT": str(self.listener.getsockname()[1])}
        self.env = mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.accepting = False
        self.listener.close()
        self.tmp.cleanup()

    def _accept(self):
        while self.accepting:
            try:
                conn, _ = self.listener.accept()
                conn.close()
            except OSError:
                return

    def start_sender(self):
        with mock.patch.object(dv, "hw_encoder_node", return_value="/dev/video11"):
            s = sender.Sender(self.cfg, test_pattern=True, heartbeat=False, ffmpeg=str(self.ffmpeg))
        thread = threading.Thread(target=s.run, daemon=True)
        thread.start()
        return s, thread

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_falls_back_to_libx264_and_streams(self):
        s, thread = self.start_sender()
        try:
            self.assertTrue(self.wait_for(lambda: s.state == "streaming"), f"state={s.state}")
            calls = self.log.read_text().splitlines()
            self.assertEqual(len(calls), 3, "hw with -g, hw without -g, then libx264")
            self.assertIn("h264_v4l2m2m", calls[0])
            self.assertIn("-g", calls[0].split())
            self.assertNotIn("-g", calls[1].split())
            self.assertIn("libx264", calls[2])
            payload = s.heartbeat_payload()
            self.assertEqual(payload["state"], "streaming")
            self.assertEqual(payload["encoder"], "libx264")
            self.assertTrue(payload["audio"])
            self.assertEqual(payload["bitrate_kbps"], 2000.0)
        finally:
            s.stop()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "stop() ends run()")
        self.assertEqual(s.state, "stopped")

    def test_restarts_a_stalled_ffmpeg(self):
        with mock.patch.dict(os.environ, {"FAKE_MODE": "stall"}), \
                mock.patch.object(sender, "STALL_S", 0.5):
            s, thread = self.start_sender()
            try:
                self.assertTrue(self.wait_for(lambda: s.restarts >= 4, timeout=15),
                                f"restarts={s.restarts}")
                self.assertIn("stalled", s.last_error)
            finally:
                s.stop()
                thread.join(timeout=10)

    def test_waits_for_the_jetson(self):
        self.accepting = False
        self.listener.close()
        s = sender.Sender({**self.cfg, "RTSP_PORT": "1"}, test_pattern=True, heartbeat=False,
                          ffmpeg=str(self.ffmpeg))
        thread = threading.Thread(target=s.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.wait_for(lambda: s.state == "waiting_for_jetson", timeout=5))
            self.assertFalse(self.log.exists(), "no ffmpeg until the Jetson answers")
        finally:
            s.stop()
            thread.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
