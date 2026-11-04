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


class FakeUpstream:
    """Stands in for MediaMTX's WebRTC/HLS servers: records the last request."""

    def __init__(self):
        self.calls = []
        self.response = (201, {"Content-Type": "application/sdp"}, b"v=0 answer")

    def __call__(self, method, url, body=None, headers=None, timeout=10.0):
        self.calls.append((method, url, body, headers))
        return self.response


@unittest.skipIf(app is None, "Flask not installed")
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.mtx = FakeMediaMTX()
        self.talk_mtx = FakeMediaMTX()
        self.upstream = FakeUpstream()
        cfg = {"STREAM_PATH": "astronaut", "TALKBACK_PATH": "talkback", "WEBRTC_PORT": "8889",
               "HLS_PORT": "8888", "WEB_TLS_PORT": "8443"}
        self.app = app.create_app(
            cfg, app.FeedMonitor("http://x", "astronaut", self.mtx, self.clock),
            app.HeartbeatStore(self.clock),
            app.FeedMonitor("http://x", "talkback", self.talk_mtx, self.clock),
            fetch=self.upstream)
        self.client = self.app.test_client()

    def test_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Astronaut feed", html)
        self.assertIn('"talkbackPath": "talkback"', html)
        self.assertIn('"tlsPort": 8443', html)
        self.assertIn("Talk-back to the astronaut", html)
        self.assertIn("player.js", html)

    def test_whep_is_passed_to_mediamtx(self):
        resp = self.client.post("/webrtc/astronaut/whep", data="v=0 offer",
                                content_type="application/sdp")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_data(), b"v=0 answer")
        method, url, body, headers = self.upstream.calls[-1]
        self.assertEqual((method, url, body), ("POST", "http://127.0.0.1:8889/astronaut/whep", b"v=0 offer"))
        self.assertEqual(headers["Content-Type"], "application/sdp")

    def test_whip_only_for_talkback(self):
        self.assertEqual(self.client.post("/webrtc/talkback/whip", data="o",
                                          content_type="application/sdp").status_code, 201)
        self.assertEqual(self.upstream.calls[-1][1], "http://127.0.0.1:8889/talkback/whip")
        calls = len(self.upstream.calls)
        for url in ("/webrtc/astronaut/whip", "/webrtc/talkback/whep", "/webrtc/other/whep"):
            self.assertEqual(self.client.post(url, data="o", content_type="application/sdp").status_code,
                             404, url)
        self.assertEqual(len(self.upstream.calls), calls, "refused requests never reach MediaMTX")

    def test_mediamtx_errors_pass_through(self):
        self.upstream.response = (404, {"Content-Type": "text/plain"}, b"no stream")
        self.assertEqual(self.client.post("/webrtc/astronaut/whep", data="o",
                                          content_type="application/sdp").status_code, 404)

    def test_hls_proxy(self):
        self.upstream.response = (200, {"Content-Type": "application/vnd.apple.mpegurl",
                                        "Cache-Control": "no-cache"}, b"#EXTM3U")
        resp = self.client.get("/hls/astronaut/index.m3u8?_HLS_msn=5")
        self.assertEqual(resp.get_data(), b"#EXTM3U")
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        self.assertEqual(self.upstream.calls[-1][1], "http://127.0.0.1:8888/astronaut/index.m3u8?_HLS_msn=5")
        self.client.get("/hls/astronaut/")
        self.assertEqual(self.upstream.calls[-1][1], "http://127.0.0.1:8888/astronaut/")
        self.assertEqual(self.client.get("/hls/other/index.m3u8").status_code, 404)

    def test_talkback_status(self):
        self.talk_mtx.response = (404, None)
        self.assertEqual(self.client.get("/api/talkback").get_json()["live"], False)
        self.clock.t += 2
        self.talk_mtx.response = (200, path_body(tracks=("Opus",), readers=1))
        st = self.client.get("/api/talkback").get_json()
        self.assertEqual((st["live"], st["listeners"]), (True, 1))
        self.assertEqual(self.client.get("/api/status").get_json()["talkback"]["listeners"], 1)

    def test_big_sdp_refused(self):
        resp = self.client.post("/webrtc/astronaut/whep", data="x" * 70_000,
                                content_type="application/sdp")
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.upstream.calls, [])

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
