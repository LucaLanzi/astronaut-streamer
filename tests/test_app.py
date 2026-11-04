"""Tests for jetson/app.py (skipped where Flask isn't installed).

    python3 -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jetson"))
try:
    import app  # noqa: E402
except ImportError as exc:  # Flask missing: only the Jetson needs it
    app = None
    IMPORT_ERROR = str(exc)


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakeMediaMTX:
    def __init__(self):
        self.response = (0, None)
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        return self.response


def path_body(ready=True, ready_time="t1", tracks=("H264", "Opus"), received=0, readers=1):
    return {"name": "astronaut", "ready": ready, "readyTime": ready_time if ready else None,
            "tracks": list(tracks), "bytesReceived": received,
            "readers": [{"type": "webRTCSession"}] * readers, "source": {"type": "rtspSession"}}


@unittest.skipIf(app is None, "Flask not installed")
class FeedMonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.mtx = FakeMediaMTX()
        self.monitor = app.FeedMonitor("http://127.0.0.1:9997", "astronaut", self.mtx, self.clock)

    def status(self, response):
        self.mtx.response = response
        self.clock.t += 2   # past the 1 s cache
        return self.monitor.status()

    def test_mediamtx_down(self):
        st = self.status((0, None))
        self.assertFalse(st["live"])
        self.assertEqual(st["mediamtx"], "down")

    def test_waiting_for_publisher(self):
        st = self.status((404, {"error": "path not found"}))
        self.assertFalse(st["live"])
        self.assertEqual(st["mediamtx"], "up")
        st = self.status((200, path_body(ready=False)))
        self.assertFalse(st["live"])

    def test_live_bitrate_uptime(self):
        st = self.status((200, path_body(received=1_000)))
        self.assertTrue(st["live"])
        self.assertTrue(st["video"] and st["audio"])
        self.assertEqual(st["viewers"], 1)
        self.assertIsNone(st["bitrate_kbps"], "needs two samples")
        st = self.status((200, path_body(received=501_000)))
        self.assertEqual(st["bitrate_kbps"], 2000)   # 500 kB in 2 s
        self.assertEqual(st["uptime_s"], 2)

    def test_video_only_flagged(self):
        st = self.status((200, path_body(tracks=("H264",))))
        self.assertTrue(st["video"])
        self.assertFalse(st["audio"])

    def test_new_session_resets_uptime(self):
        self.status((200, path_body(ready_time="t1")))
        self.status((200, path_body(ready_time="t1")))
        st = self.status((200, path_body(ready_time="t2")))
        self.assertEqual(st["uptime_s"], 0)
        self.assertEqual(st["session"], "t2")

    def test_cached_for_a_second(self):
        self.status((200, path_body()))
        calls = self.mtx.calls
        self.clock.t += 0.5
        self.monitor.status()
        self.assertEqual(self.mtx.calls, calls)

    def test_track_objects(self):
        """Tracks given as objects instead of names are understood too."""
        body = {**path_body(), "tracks": [{"codec": "H264"}, {"codec": "Opus"}]}
        st = self.status((200, body))
        self.assertEqual(st["tracks"], ["H264", "Opus"])


@unittest.skipIf(app is None, "Flask not installed")
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.mtx = FakeMediaMTX()
        cfg = {"STREAM_PATH": "astronaut", "WEBRTC_PORT": "8889", "HLS_PORT": "8888"}
        self.app = app.create_app(cfg, app.FeedMonitor("http://x", "astronaut", self.mtx, self.clock),
                                  app.HeartbeatStore(self.clock))
        self.client = self.app.test_client()

    def test_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Astronaut feed", html)
        self.assertIn('"webrtcPort": 8889', html)
        self.assertIn("player.js", html)

    def test_static(self):
        for name in ("player.js", "style.css"):
            self.assertEqual(self.client.get(f"/static/{name}").status_code, 200)

    def test_status_and_heartbeat(self):
        st = self.client.get("/api/status").get_json()
        self.assertIsNone(st["pi"])
        self.assertFalse(st["feed"]["live"])
        resp = self.client.post("/api/heartbeat", json={"state": "streaming", "ip": "10.42.0.2"})
        self.assertEqual(resp.status_code, 200)
        self.clock.t += 2
        pi = self.client.get("/api/status").get_json()["pi"]
        self.assertEqual(pi["state"], "streaming")
        self.assertEqual(pi["age_s"], 2)
        self.assertFalse(pi["stale"])
        self.clock.t += 10
        self.assertTrue(self.client.get("/api/status").get_json()["pi"]["stale"])

    def test_bad_heartbeats(self):
        self.assertEqual(self.client.post("/api/heartbeat", data="nope",
                                          content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/heartbeat", json=[1, 2]).status_code, 400)
        big = json.dumps({"x": "a" * 70_000})
        self.assertEqual(self.client.post("/api/heartbeat", data=big,
                                          content_type="application/json").status_code, 413)

    def test_healthz(self):
        self.assertEqual(self.client.get("/healthz").get_data(as_text=True), "ok\n")


if __name__ == "__main__":
    unittest.main()
