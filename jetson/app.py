#!/usr/bin/env python3
"""Ground-station dashboard on the Jetson (PLAN.md §3.4, §3.6).

Serves the page the ground station opens. The page plays the astronaut feed
from MediaMTX (WebRTC, with an HLS fallback) and, on the HTTPS port, sends the
ground station's microphone to the Pi (talk-back). All of the browser's
requests come here: WebRTC set-up and HLS are passed on to MediaMTX, so an
HTTPS page never makes an insecure request. Only the WebRTC media itself (UDP)
flows straight between the browser and MediaMTX.

    GET  /                       dashboard
    GET  /api/status             stream, talk-back and Pi status (JSON)
    GET  /api/talkback           {"live", "listeners"}: the Pi's talk-back player polls this
    POST /api/heartbeat          the Pi's sender posts its health here every 2 s
    POST /webrtc/<path>/whep     start watching a stream (passed to MediaMTX)
    POST /webrtc/<path>/whip     start publishing talk-back (passed to MediaMTX)
    GET  /hls/<path>/...         MediaMTX's HLS player and playlists (passed on)
    GET  /tls.crt                the dashboard's self-signed certificate
    GET  /healthz                "ok"

The same app is served over HTTP on WEB_PORT (waitress: viewing, and the Pi)
and over HTTPS on WEB_TLS_PORT (cheroot: viewing + talk-back, because browsers
only allow the microphone on HTTPS pages). Runs on Ubuntu 22.04's Flask 2.0.1,
waitress 1.4.4 and cheroot 8.5.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
from envconfig import get_int, load_config  # noqa: E402

log = logging.getLogger("astronaut-web")

HEARTBEAT_STALE_S = 6.0     # Pi heartbeats come every 2 s
MAX_BODY_BYTES = 64 * 1024  # heartbeats and SDP offers are a few kB
TLS_DIR = Path(os.environ.get("ASTRO_TLS_DIR", "/etc/astronaut-streamer/tls"))
POLL_MIN_INTERVAL_S = 1.0   # ask MediaMTX at most this often, however many viewers


def http_get_json(url: str, timeout: float = 1.0) -> tuple[int, dict | None]:
    """(HTTP status, JSON body). Status 0 means MediaMTX didn't answer at all."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"null")
        except ValueError:
            body = None
        return exc.code, body
    except (OSError, ValueError):
        return 0, None


def upstream(method: str, url: str, body: bytes | None = None,
             headers: dict | None = None, timeout: float = 10.0) -> tuple[int, dict, bytes]:
    """Pass a request on to MediaMTX: (status, headers, body). 502 if it's down."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()
    except OSError as exc:
        return 502, {"Content-Type": "text/plain"}, f"media server unreachable: {exc}\n".encode()


VIDEO_CODECS = {"H264", "H265", "AV1", "VP8", "VP9", "M-JPEG", "MPEG-4 Video", "MPEG-1/2 Video"}
AUDIO_CODECS = {"Opus", "MPEG-4 Audio", "MPEG-1/2 Audio", "G711", "G722", "LPCM", "AC-3"}


class FeedMonitor:
    """Stream status from MediaMTX's API, cached for a second, with bitrate
    and uptime worked out between polls."""

    def __init__(self, api_base: str, path: str, fetch=http_get_json, clock=time.monotonic):
        self.url = f"{api_base}/v3/paths/get/{path}"
        self.fetch = fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.cached: tuple[float, dict] | None = None
        self.prev_bytes: tuple[float, int] | None = None
        self.live_since: float | None = None
        self.ready_time: str | None = None

    def status(self) -> dict:
        with self.lock:
            now = self.clock()
            if self.cached and now - self.cached[0] < POLL_MIN_INTERVAL_S:
                return self.cached[1]
            code, body = self.fetch(self.url)
            result = self._summarize(code, body, now)
            self.cached = (now, result)
            return result

    def _summarize(self, code: int, body: dict | None, now: float) -> dict:
        if code == 0:
            self._went_offline()
            return {"live": False, "mediamtx": "down",
                    "message": "media server not responding"}
        if code != 200 or not isinstance(body, dict):
            # MediaMTX answers 404 for a path nobody is publishing to.
            self._went_offline()
            return {"live": False, "mediamtx": "up", "message": "waiting for astronaut feed"}

        live = bool(body.get("ready"))
        tracks = [t if isinstance(t, str) else str(t.get("codec", t)) for t in body.get("tracks") or []]
        received = int(body.get("bytesReceived") or body.get("inboundBytes") or 0)
        bitrate = None
        if live and self.prev_bytes and received >= self.prev_bytes[1] and now > self.prev_bytes[0]:
            bitrate = (received - self.prev_bytes[1]) * 8 / 1000 / (now - self.prev_bytes[0])
        self.prev_bytes = (now, received) if live else None

        ready_time = body.get("readyTime")
        if not live:
            self._went_offline()
        elif self.live_since is None or ready_time != self.ready_time:
            self.live_since = now   # a new publisher session started
        self.ready_time = ready_time if live else None

        return {
            "live": live,
            "mediamtx": "up",
            "message": "live" if live else "waiting for astronaut feed",
            "session": ready_time,  # changes when the Pi reconnects
            "tracks": tracks,
            "video": any(t in VIDEO_CODECS for t in tracks),
            "audio": any(t in AUDIO_CODECS for t in tracks),
            "bitrate_kbps": round(bitrate) if bitrate is not None else None,
            "uptime_s": round(now - self.live_since) if live and self.live_since else 0,
            "viewers": len(body.get("readers") or []),
            "source": (body.get("source") or {}).get("type"),
        }

    def _went_offline(self) -> None:
        self.live_since = None
        self.prev_bytes = None
        self.ready_time = None


class HeartbeatStore:
    """The Pi's latest heartbeat."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.data: dict | None = None
        self.received: float | None = None
        self.remote: str | None = None

    def update(self, data: dict, remote: str | None) -> None:
        with self.lock:
            self.data, self.received, self.remote = data, self.clock(), remote

    def snapshot(self) -> dict | None:
        with self.lock:
            if self.data is None:
                return None
            age = self.clock() - self.received
            return {**self.data, "age_s": round(age, 1), "stale": age > HEARTBEAT_STALE_S,
                    "remote": self.remote}


def page_config(cfg: dict) -> dict:
    """What the page's JavaScript needs."""
    return {
        "path": cfg.get("STREAM_PATH") or "astronaut",
        "talkbackPath": cfg.get("TALKBACK_PATH") or "talkback",
        "tlsPort": get_int(cfg, "WEB_TLS_PORT", 8443),
    }


def create_app(cfg: dict, monitor: FeedMonitor | None = None,
               heartbeats: HeartbeatStore | None = None,
               talkback: FeedMonitor | None = None, fetch=upstream) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
    path = cfg.get("STREAM_PATH") or "astronaut"
    talkback_path = cfg.get("TALKBACK_PATH") or "talkback"
    api = f"http://127.0.0.1:{get_int(cfg, 'MEDIAMTX_API_PORT', 9997)}"
    webrtc = f"http://127.0.0.1:{get_int(cfg, 'WEBRTC_PORT', 8889)}"
    hls = f"http://127.0.0.1:{get_int(cfg, 'HLS_PORT', 8888)}"
    monitor = monitor or FeedMonitor(api, path)
    talkback = talkback or FeedMonitor(api, talkback_path)
    heartbeats = heartbeats or HeartbeatStore()
    # </script> can't appear inside the inline JSON block.
    config_json = json.dumps(page_config(cfg)).replace("</", "<\\/")

    @app.before_request
    def limit_body():
        # Also checked here: Flask 2.0 (Ubuntu 22.04) doesn't apply
        # MAX_CONTENT_LENGTH to every kind of body.
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return jsonify({"error": "request too large"}), 413
        return None

    @app.route("/")
    def index():
        return render_template("index.html", page_config=config_json)

    def talkback_state() -> dict:
        st = talkback.status()
        # A reader of the talk-back stream is the Pi's player.
        return {"live": st["live"], "listeners": st.get("viewers", 0) if st["live"] else 0,
                "mediamtx": st["mediamtx"]}

    @app.route("/api/status")
    def api_status():
        resp = jsonify({"feed": monitor.status(), "talkback": talkback_state(),
                        "pi": heartbeats.snapshot(), "stream_path": path, "time": time.time()})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/talkback")
    def api_talkback():
        resp = jsonify(talkback_state())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/heartbeat", methods=["POST"])
    def api_heartbeat():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        heartbeats.update(data, request.remote_addr)
        return jsonify({"ok": True})

    @app.route("/webrtc/<name>/<kind>", methods=["POST"])
    def webrtc_signal(name, kind):
        # Only watching the feed and publishing talk-back.
        if (name, kind) not in {(path, "whep"), (talkback_path, "whip")}:
            abort(404)
        status, headers, body = fetch(
            "POST", f"{webrtc}/{name}/{kind}", request.get_data(),
            {"Content-Type": request.content_type or "application/sdp"})
        return Response(body, status=status,
                        content_type=headers.get("Content-Type", "application/sdp"))

    @app.route("/hls/<name>/", defaults={"rest": ""})
    @app.route("/hls/<name>/<path:rest>")
    def hls_proxy(name, rest):
        if name != path:
            abort(404)
        query = request.query_string.decode()
        # Low-latency HLS holds playlist requests open until new media exists.
        status, headers, body = fetch(
            "GET", f"{hls}/{name}/{rest}" + (f"?{query}" if query else ""), timeout=20)
        resp = Response(body, status=status, content_type=headers.get("Content-Type"))
        if "Cache-Control" in headers:
            resp.headers["Cache-Control"] = headers["Cache-Control"]
        return resp

    @app.route("/tls.crt")
    def tls_cert():
        cert = TLS_DIR / "dashboard.crt"
        if not cert.is_file():
            abort(404)
        return send_file(str(cert), mimetype="application/x-x509-ca-cert")

    @app.route("/healthz")
    def healthz():
        return "ok\n", 200, {"Content-Type": "text/plain"}

    return app


def ipv4_addresses() -> list[tuple[str, str]]:
    """(interface, address) for every non-loopback IPv4 address."""
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    result = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] != "lo":
            result.append((parts[1], parts[3].split("/")[0]))
    return result


def serve_https(app: Flask, port: int) -> bool:
    """Serve the app over HTTPS in a background thread. False if it can't
    (no certificate, or cheroot missing): HTTP still works, without talk-back."""
    cert, key = TLS_DIR / "dashboard.crt", TLS_DIR / "dashboard.key"
    if not (cert.is_file() and key.is_file()):
        log.warning("no certificate in %s: HTTPS (talk-back) is off; re-run the installer", TLS_DIR)
        return False
    try:
        from cheroot import wsgi
        from cheroot.ssl.builtin import BuiltinSSLAdapter
    except ImportError:
        log.warning("cheroot isn't installed: HTTPS (talk-back) is off (sudo apt install python3-cheroot)")
        return False
    server = wsgi.Server(("0.0.0.0", port), app, numthreads=16, server_name="astronaut-streamer")
    server.ssl_adapter = BuiltinSSLAdapter(str(cert), str(key))
    threading.Thread(target=server.start, name="https", daemon=True).start()
    return True


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    port = get_int(cfg, "WEB_PORT", 8080)
    tls_port = get_int(cfg, "WEB_TLS_PORT", 8443)
    app = create_app(cfg)
    https = serve_https(app, tls_port)
    for iface, addr in ipv4_addresses():
        note = "  <- ground station" if addr == cfg.get("JETSON_GROUND_IP") else ""
        log.info("dashboard: http://%s:%d%s  (%s)%s", addr, port,
                 f"  talk-back: https://{addr}:{tls_port}" if https else "", iface, note)
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=16, ident="astronaut-streamer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
