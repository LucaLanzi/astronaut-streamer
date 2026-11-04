"""Unit tests for pi/devices.py and pi/probe_devices.py.

Run from the repo root (standard library only):

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pi"))
import devices as dv  # noqa: E402
import probe_devices  # noqa: E402

V4L2_FORMATS = """\
ioctl: VIDIOC_ENUM_FMT
	Type: Video Capture

	[0]: 'YUYV' (YUYV 4:2:2)
		Size: Discrete 640x480
			Interval: Discrete 0.033s (30.000 fps)
			Interval: Discrete 0.067s (15.000 fps)
		Size: Discrete 1280x720
			Interval: Discrete 0.100s (10.000 fps)
	[1]: 'MJPG' (Motion-JPEG, compressed)
		Size: Discrete 1920x1080
			Interval: Discrete 0.033s (30.000 fps)
		Size: Discrete 1280x720
			Interval: Discrete 0.033s (30.000 fps)
			Interval: Discrete 0.042s (24.000 fps)
		Size: Discrete 640x480
			Interval: Discrete 0.033s (30.000 fps)
	[2]: 'H264' (H.264, compressed)
		Size: Discrete 1280x720
			Interval: Discrete 0.033s (30.000 fps)
	[3]: 'GREY' (8-bit Greyscale)
		Size: Stepwise 16x16 - 640x480 with step 1/1
			Interval: Stepwise 0.033s - 1.000s with step 0.000s (1.000-30.000 fps)
"""

PROC_ASOUND_CARDS = """\
 0 [Headphones     ]: bcm2835_headpho - bcm2835 Headphones
                      bcm2835 Headphones
 1 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 2 [C920           ]: USB-Audio - HD Pro Webcam C920
                      HD Pro Webcam C920 at usb-0000:01:00.0-1.2, high speed
 3 [UACDemoV10     ]: USB-Audio - UACDemoV1.0
                      Jieli Technology UACDemoV1.0 at usb-0000:01:00.0-1.3, full speed
"""

USB = "/sys/devices/platform/scb/fd500000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0/usb1"


def mode(fourcc, w, h, fps):
    return dv.VideoMode(fourcc, w, h, fps)


def card(index, card_id, usb=None, capture=(), playback=(), name=None):
    return dv.SoundCard(index, card_id, "USB-Audio" if usb else "other", name or card_id,
                        "", tuple(capture), tuple(playback), usb)


class ConfigTests(unittest.TestCase):
    def test_parse_env(self):
        text = "# comment\n\nA=1\nexport B=two\nC='three words'\nD=\"4\"\nE=\nnot a line\n1X=bad\n"
        self.assertEqual(dv.parse_env(text),
                         {"A": "1", "B": "two", "C": "three words", "D": "4", "E": ""})

    def test_load_config_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "astronaut.env").write_text("VIDEO_WIDTH=1280\nVIDEO_FPS=30\nHOTSPOT_PSK=\n")
            Path(tmp, "astronaut.local.env").write_text("HOTSPOT_PSK=secret123\nVIDEO_FPS=25\n")
            with mock.patch.dict(os.environ, {"VIDEO_FPS": "15"}):
                cfg = dv.load_config(Path(tmp))
        self.assertEqual(cfg, {"VIDEO_WIDTH": "1280", "VIDEO_FPS": "15", "HOTSPOT_PSK": "secret123"})

    def test_shipped_config_parses(self):
        cfg = dv.parse_env((dv.CONFIG_DIR / "astronaut.env").read_text())
        self.assertEqual(cfg["JETSON_GROUND_IP"], "10.110.0.230")
        self.assertEqual(cfg["PI_HOTSPOT_IP"], "10.42.0.2")
        self.assertEqual(cfg["HOTSPOT_PSK"], "")


class VideoTests(unittest.TestCase):
    def test_parse_v4l2_formats(self):
        modes = dv.parse_v4l2_formats(V4L2_FORMATS)
        self.assertIn(mode("MJPG", 1280, 720, 30.0), modes)
        self.assertIn(mode("YUYV", 1280, 720, 10.0), modes)
        self.assertIn(mode("H264", 1280, 720, 30.0), modes)
        self.assertFalse([m for m in modes if m.fourcc == "GREY"], "stepwise sizes are skipped")
        self.assertEqual(len(modes), 8)

    def test_pick_mode_exact(self):
        modes = dv.parse_v4l2_formats(V4L2_FORMATS)
        self.assertEqual(dv.pick_mode(modes, 1280, 720, 30), mode("MJPG", 1280, 720, 30.0))

    def test_pick_mode_prefers_frame_rate_over_size(self):
        modes = [mode("MJPG", 1280, 720, 15), mode("MJPG", 960, 540, 30)]
        self.assertEqual(dv.pick_mode(modes, 1280, 720, 30), mode("MJPG", 960, 540, 30))

    def test_pick_mode_tolerates_ntsc_rates(self):
        modes = [mode("MJPG", 1280, 720, 29.97), mode("MJPG", 640, 480, 30)]
        self.assertEqual(dv.pick_mode(modes, 1280, 720, 30), mode("MJPG", 1280, 720, 29.97))

    def test_pick_mode_prefers_mjpeg_over_raw(self):
        modes = [mode("YUYV", 1280, 720, 30), mode("MJPG", 640, 480, 30)]
        self.assertEqual(dv.pick_mode(modes, 1280, 720, 30).fourcc, "MJPG")

    def test_pick_mode_raw_only_and_none(self):
        self.assertEqual(dv.pick_mode([mode("YUYV", 640, 480, 30)], 1280, 720, 30).fourcc, "YUYV")
        self.assertIsNone(dv.pick_mode([mode("H264", 1280, 720, 30)], 1280, 720, 30))

    def test_pick_camera(self):
        a = dv.Camera("/dev/v4l/by-id/usb-Foo-video-index0", "/dev/video0", "Foo", None, [])
        b = dv.Camera("/dev/v4l/by-id/usb-046d_C920-video-index0", "/dev/video2", "HD Pro Webcam C920",
                      None, [mode("MJPG", 1280, 720, 30)])
        self.assertIs(dv.pick_camera([a, b]), b, "first camera with a usable mode")
        self.assertIs(dv.pick_camera([a, b], "foo"), a)
        self.assertIsNone(dv.pick_camera([a, b], "Brio"))
        self.assertIsNone(dv.pick_camera([]))

    def test_camera_input_uses_stable_path(self):
        cam = dv.Camera("/dev/v4l/by-id/usb-C920-video-index0", "/dev/video0", "C920", None)
        args = dv.camera_input(cam, mode("MJPG", 1280, 720, 30))
        self.assertEqual(args[-1], "/dev/v4l/by-id/usb-C920-video-index0")
        self.assertEqual(args[args.index("-input_format") + 1], "mjpeg")
        self.assertEqual(args[args.index("-framerate") + 1], "30")


class UsbTests(unittest.TestCase):
    def test_usb_device_of(self):
        self.assertEqual(dv.usb_device_of(f"{USB}/1-1/1-1.2/1-1.2:1.0"), f"{USB}/1-1/1-1.2")
        self.assertEqual(dv.usb_device_of(f"{USB}/1-1/1-1.2/1-1.2.4/1-1.2.4:1.3"),
                         f"{USB}/1-1/1-1.2/1-1.2.4")
        self.assertIsNone(dv.usb_device_of("/sys/devices/platform/soc/fe00b840.mailbox/bcm2835-codec"))


class AudioTests(unittest.TestCase):
    cam_usb = f"{USB}/1-1/1-1.2"
    camera_mic = card(2, "C920", cam_usb, capture=(0,))
    speaker = card(3, "UACDemoV10", f"{USB}/1-1/1-1.3", playback=(0,))
    speakerphone = card(4, "Jabra", f"{USB}/1-1/1-1.4", capture=(0,), playback=(0,))
    hdmi = card(1, "vc4hdmi0", None, playback=(0,))

    def test_parse_proc_asound_cards(self):
        cards = dv.parse_proc_asound_cards(PROC_ASOUND_CARDS)
        self.assertEqual([c["id"] for c in cards], ["Headphones", "vc4hdmi0", "C920", "UACDemoV10"])
        self.assertEqual(cards[2]["name"], "HD Pro Webcam C920")
        self.assertIn("usb-0000:01:00.0-1.2", cards[2]["longname"])
        self.assertEqual(dv.parse_proc_asound_cards("--- no soundcards ---"), [])

    def test_alsa_device(self):
        self.assertEqual(self.camera_mic.alsa_device("capture"), "plughw:CARD=C920,DEV=0")
        self.assertEqual(card(5, "X", capture=(1,)).alsa_device("capture"), "plughw:CARD=X,DEV=1")

    def test_mic_on_camera_wins_over_speakerphone(self):
        cards = [self.hdmi, self.speakerphone, self.camera_mic, self.speaker]
        mic, why = dv.pick_mic(cards, self.cam_usb)
        self.assertIs(mic, self.camera_mic)
        self.assertIn("same USB device", why)

    def test_mic_only_usb_mic_elsewhere(self):
        lone = card(2, "C920", f"{USB}/1-1/1-1.2/1-1.2.2", capture=(0,))
        mic, why = dv.pick_mic([lone, self.speaker], self.cam_usb)
        self.assertIs(mic, lone)
        self.assertIn("only USB mic", why)

    def test_mic_ambiguous_returns_none(self):
        other = card(2, "C920", f"{USB}/1-1/1-1.2/1-1.2.2", capture=(0,))
        mic, why = dv.pick_mic([other, self.speakerphone], self.cam_usb)
        self.assertIsNone(mic)
        self.assertIn("MIC_MATCH", why)

    def test_mic_match_and_none(self):
        mic, _ = dv.pick_mic([self.camera_mic, self.speakerphone], self.cam_usb, "jabra")
        self.assertIs(mic, self.speakerphone)
        self.assertIsNone(dv.pick_mic([self.speaker, self.hdmi], self.cam_usb)[0])

    def test_speaker_excludes_camera_and_hdmi(self):
        cam_with_playback = card(2, "C920", self.cam_usb, capture=(0,), playback=(0,))
        spk, _ = dv.pick_speaker([self.hdmi, cam_with_playback, self.speaker], self.cam_usb)
        self.assertIs(spk, self.speaker)
        self.assertIsNone(dv.pick_speaker([self.hdmi, cam_with_playback], self.cam_usb)[0])
        self.assertIs(dv.pick_speaker([self.hdmi, self.speaker], self.cam_usb, "hdmi")[0], self.hdmi)


class SysfsTests(unittest.TestCase):
    """list_sound_cards / list_cameras against a fake /proc + /sys + /dev tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        usb = self.root / "devices" / "usb1" / "1-1"
        for iface in ("1-1.2/1-1.2:1.0", "1-1.2/1-1.2:1.2", "1-1.3/1-1.3:1.0"):
            (usb / iface).mkdir(parents=True)
        self.usb = usb

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_sound_cards(self):
        proc = self.root / "proc"
        sysfs = self.root / "sound"
        for d in ("card0/pcm0p", "card2/pcm0c", "card3/pcm0p"):
            (proc / d).mkdir(parents=True)
        (proc / "cards").write_text(PROC_ASOUND_CARDS)
        for index, target in ((2, "1-1.2/1-1.2:1.2"), (3, "1-1.3/1-1.3:1.0")):
            (sysfs / f"card{index}").mkdir(parents=True)
            (sysfs / f"card{index}" / "device").symlink_to(self.usb / target)
        cards = {c.id: c for c in dv.list_sound_cards(str(proc), str(sysfs))}
        self.assertTrue(cards["C920"].capture and not cards["C920"].playback)
        self.assertEqual(cards["C920"].usb_device, str(self.usb / "1-1.2"))
        self.assertEqual(cards["UACDemoV10"].usb_device, str(self.usb / "1-1.3"))
        self.assertIsNone(cards["Headphones"].usb_device)
        self.assertFalse(cards["vc4hdmi0"].playback, "no pcm dirs created for it")

    def test_list_cameras(self):
        by_id = self.root / "by-id"
        sysfs = self.root / "video4linux"
        dev = self.root / "dev"
        for d in (by_id, dev):
            d.mkdir()
        for node, target, index, name in (("video0", "1-1.2/1-1.2:1.0", "0", "HD Pro Webcam C920"),
                                          ("video1", "1-1.2/1-1.2:1.0", "1", "HD Pro Webcam C920"),
                                          ("video11", None, "0", "bcm2835-codec-encode")):
            node_dir = sysfs / node
            node_dir.mkdir(parents=True)
            (node_dir / "index").write_text(index)
            (node_dir / "name").write_text(name + "\n")
            if target:
                (node_dir / "device").symlink_to(self.usb / target)
            (dev / node).touch()
        (by_id / "usb-046d_C920-video-index0").symlink_to(dev / "video0")
        (by_id / "usb-046d_C920-video-index1").symlink_to(dev / "video1")
        cameras = dv.list_cameras(str(by_id), str(sysfs), query_modes=False)
        self.assertEqual(len(cameras), 1, "metadata node and Pi codec node are not cameras")
        self.assertEqual(cameras[0].name, "HD Pro Webcam C920")
        self.assertEqual(cameras[0].usb_device, str(self.usb / "1-1.2"))
        self.assertEqual(dv.hw_encoder_node(str(sysfs)), "/dev/video11")


class FfmpegTests(unittest.TestCase):
    def test_parse_encoders(self):
        text = (" V..... = Video\n ------\n V....D libx264              libx264 H.264\n"
                " V..... h264_v4l2m2m         V4L2 mem2mem H.264 encoder wrapper\n"
                " A....D libopus              libopus Opus\n")
        self.assertEqual(dv.parse_encoders(text), {"libx264", "h264_v4l2m2m", "libopus"})

    def test_encoder_args(self):
        x264 = dv.encoder_args("libx264", "2M", 30)
        self.assertEqual(x264[x264.index("-g") + 1], "30")
        self.assertIn("zerolatency", x264)
        self.assertEqual(x264[x264.index("-bf") + 1], "0")
        hw = dv.encoder_args("h264_v4l2m2m", "1M", 25, gop=False)
        self.assertNotIn("-g", hw)
        self.assertEqual(hw[hw.index("-pix_fmt") + 1], "yuv420p")

    def test_parse_progress_takes_last_block(self):
        text = "frame=10\nspeed=0.5x\nprogress=continue\nframe=150\nspeed=2.53x\nprogress=end\n"
        self.assertEqual(dv.parse_progress(text), {"frame": "150", "speed": "2.53x", "progress": "end"})
        self.assertEqual(dv.parse_progress(""), {})


class HealthTests(unittest.TestCase):
    def test_decode_throttled(self):
        self.assertEqual(dv.decode_throttled(0), [])
        self.assertEqual(dv.decode_throttled(0x50005), ["under-voltage now", "throttled now",
                                                        "under-voltage has occurred",
                                                        "throttling has occurred"])

    def test_parse_iw_link(self):
        text = ("Connected to 48:b0:2d:11:22:33 (on wlan0)\n\tSSID: ASTRO-LINK\n\tfreq: 2437.0\n"
                "\tRX: 1234 bytes (10 packets)\n\tsignal: -48 dBm\n\ttx bitrate: 72.2 MBit/s\n")
        self.assertEqual(dv.parse_iw_link(text), {"ssid": "ASTRO-LINK", "freq_mhz": 2437.0,
                                                  "signal_dbm": -48, "tx_bitrate": "72.2 MBit/s"})
        self.assertEqual(dv.parse_iw_link("Not connected.\n"), {})


class ProbeTests(unittest.TestCase):
    def test_summarize_modes(self):
        summary = probe_devices.summarize_modes(dv.parse_v4l2_formats(V4L2_FORMATS))
        self.assertIn("MJPG: 1920x1080@30, 1280x720@30, 640x480@30", summary)
        self.assertIn("YUYV: 1280x720@10, 640x480@30", summary)


if __name__ == "__main__":
    unittest.main()
