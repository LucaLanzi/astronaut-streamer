// Astronaut feed dashboard: plays the stream from MediaMTX over WebRTC
// (WHEP), with MediaMTX's own HLS page as a fallback, and shows stream and
// Pi status from /api/status. No external scripts: the ground station may
// have no internet.
"use strict";

(() => {
  const cfg = JSON.parse(document.getElementById("page-config").textContent);
  // Same host the ground station used to open this page, so no IPs are hardcoded.
  const host = window.location.hostname;
  const whepUrl = `http://${host}:${cfg.webrtcPort}/${cfg.path}/whep`;
  const hlsUrl = `http://${host}:${cfg.hlsPort}/${cfg.path}/`;

  const $ = (id) => document.getElementById(id);
  const video = $("video");
  const hls = $("hls");
  const overlay = $("overlay");
  const unmute = $("unmute");

  let mode = loadMode();
  let pc = null;            // the current RTCPeerConnection
  let connecting = false;
  let retryTimer = null;
  let feed = { live: false };
  let session = null;       // MediaMTX's readyTime: changes when the Pi reconnects
  let connects = 0;         // connection attempts so far

  // For poking at from the browser console (and tools/e2e_local.py).
  window.astro = { get pc() { return pc; }, get connects() { return connects; }, connect: () => connect() };

  function loadMode() {
    try { return localStorage.getItem("astro-mode") === "hls" ? "hls" : "webrtc"; } catch { return "webrtc"; }
  }
  function saveMode(m) {
    try { localStorage.setItem("astro-mode", m); } catch { /* private mode: fine */ }
  }

  function showOverlay(text) { overlay.textContent = text; overlay.hidden = false; }
  function hideOverlay() { overlay.hidden = true; }
  function waitingText() {
    return feed.mediamtx === "down" ? "Media server not responding" : "Waiting for astronaut feed…";
  }

  // ------------------------------------------------------------------ WebRTC

  function waitForIce(peer, ms) {
    if (peer.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { peer.removeEventListener("icegatheringstatechange", check); resolve(); };
      const check = () => { if (peer.iceGatheringState === "complete") done(); };
      peer.addEventListener("icegatheringstatechange", check);
      setTimeout(done, ms);
    });
  }

  async function connect() {
    if (mode !== "webrtc" || connecting) return;
    connecting = true;
    clearTimeout(retryTimer);
    teardown();
    showOverlay("Connecting…");
    connects += 1;
    const peer = new RTCPeerConnection();
    pc = peer;
    try {
      peer.addTransceiver("video", { direction: "recvonly" });
      peer.addTransceiver("audio", { direction: "recvonly" });
      const stream = new MediaStream();
      peer.ontrack = (ev) => {
        stream.addTrack(ev.track);
        if (video.srcObject !== stream) video.srcObject = stream;
        video.play().catch(() => { /* muted autoplay is allowed; ignore races */ });
        updateUnmute();
      };
      peer.onconnectionstatechange = () => {
        if (peer !== pc) return;
        const state = peer.connectionState;
        if (state === "connected") {
          hideOverlay();
          updateUnmute();
        } else if (state === "failed") {
          retry("connection failed");
        } else if (state === "disconnected") {
          // Often recovers by itself; give it a moment.
          setTimeout(() => {
            if (peer === pc && peer.connectionState === "disconnected") retry("connection lost");
          }, 3000);
        }
      };
      await peer.setLocalDescription(await peer.createOffer());
      await waitForIce(peer, 1500);
      const res = await fetch(whepUrl, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: peer.localDescription.sdp,
      });
      if (res.status !== 201) {
        throw new Error(res.status === 404 ? "no feed yet" : `media server answered ${res.status}`);
      }
      await peer.setRemoteDescription({ type: "answer", sdp: await res.text() });
    } catch (err) {
      if (peer === pc) retry(err && err.message ? err.message : String(err));
    } finally {
      connecting = false;
    }
  }

  // Closing the peer connection is enough: MediaMTX ends the WHEP session as
  // soon as the browser's side closes, so no separate DELETE is needed.
  function teardown() {
    if (pc) {
      const old = pc;
      pc = null;
      old.close();
    }
  }

  function retry(reason) {
    if (mode !== "webrtc") return;
    console.warn("WebRTC:", reason);
    teardown();
    clearTimeout(retryTimer);
    if (!feed.live) {
      showOverlay(waitingText());   // the status poll reconnects when the feed is back
      return;
    }
    showOverlay("Reconnecting…");
    retryTimer = setTimeout(connect, 2000);
  }

  function updateUnmute() {
    const hasAudio = video.srcObject && video.srcObject.getAudioTracks().length > 0;
    unmute.hidden = !(mode === "webrtc" && video.muted && hasAudio);
  }

  unmute.addEventListener("click", () => {
    video.muted = false;
    video.play().catch(() => {});
    updateUnmute();
  });
  video.addEventListener("volumechange", updateUnmute);

  // -------------------------------------------------------------------- modes

  function setMode(m) {
    mode = m;
    saveMode(m);
    $("btn-webrtc").classList.toggle("active", m === "webrtc");
    $("btn-hls").classList.toggle("active", m === "hls");
    if (m === "hls") {
      clearTimeout(retryTimer);
      teardown();
      video.hidden = true;
      unmute.hidden = true;
      hideOverlay();
      hls.hidden = false;
      hls.src = hlsUrl;
    } else {
      hls.src = "about:blank";
      hls.hidden = true;
      video.hidden = false;
      if (feed.live) connect(); else showOverlay(waitingText());
    }
  }

  $("btn-webrtc").addEventListener("click", () => setMode("webrtc"));
  $("btn-hls").addEventListener("click", () => setMode("hls"));
  $("btn-fullscreen").addEventListener("click", () => {
    const screen = $("screen");
    if (screen.requestFullscreen) screen.requestFullscreen().catch(() => {});
  });

  // ------------------------------------------------------------------ status

  function setBadge(el, text, level) {
    el.textContent = text;
    el.className = `badge ${level}`;
  }

  function duration(s) {
    if (!s && s !== 0) return "—";
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h} h ${m} min` : m ? `${m} min ${sec} s` : `${sec} s`;
  }

  function signalWord(dbm) {
    if (dbm >= -60) return "good";
    if (dbm >= -70) return "fair";
    if (dbm >= -80) return "weak";
    return "very weak";
  }

  const STATES = {
    streaming: "streaming",
    starting: "starting…",
    restarting: "restarting…",
    waiting_for_jetson: "waiting for the Jetson",
    no_camera: "NO CAMERA",
    stopped: "stopped",
  };

  // rows: [label, value, level?] with level "warn" | "bad"
  function fill(id, rows) {
    const dl = $(id);
    dl.replaceChildren();
    for (const [label, value, level] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      if (level) dd.className = level;
      dl.append(dt, dd);
    }
  }

  function render(st) {
    const f = st.feed || {};
    if (f.live) setBadge($("badge-feed"), "LIVE", "ok");
    else if (f.mediamtx === "down") setBadge($("badge-feed"), "SERVER DOWN", "bad");
    else setBadge($("badge-feed"), "OFFLINE", "warn");
    $("badge-audio").hidden = !(f.live && !f.audio);

    fill("stream-info", [
      ["Status", f.message || "—", f.live ? null : "warn"],
      ["Uptime", f.live ? duration(f.uptime_s) : "—"],
      ["Bitrate", f.bitrate_kbps != null ? `${f.bitrate_kbps} kbit/s` : "—"],
      ["Tracks", (f.tracks || []).join(" + ") || "—", f.live && !f.audio ? "warn" : null],
      ["Viewers", f.live ? String(f.viewers) : "—"],
      ["Player", mode === "webrtc" ? "WebRTC (low latency)" : "HLS (2–4 s delay)"],
    ]);

    const p = st.pi;
    $("badge-pi").hidden = !(p && p.stale);
    if (!p) {
      fill("pi-info", [["Status", "no heartbeat yet", "warn"]]);
      return;
    }
    const sig = p.wifi && p.wifi.signal_dbm != null ? p.wifi.signal_dbm : null;
    const flags = p.throttle_flags || [];
    fill("pi-info", [
      ["Status", p.stale ? `silent for ${Math.round(p.age_s)} s` : (STATES[p.state] || p.state || "—"),
        p.stale || p.state === "no_camera" ? "bad" : p.state === "streaming" ? null : "warn"],
      ["Address", p.ip || p.remote || "—"],
      ["Wi-Fi signal", sig != null ? `${sig} dBm (${signalWord(sig)})` : "—", sig != null && sig < -75 ? "warn" : null],
      ["CPU temp", p.temp_c != null ? `${p.temp_c.toFixed(1)} °C` : "—", p.temp_c >= 75 ? "warn" : null],
      ["Throttling", flags.length ? flags.join(", ") : "none", flags.some((x) => x.endsWith("now")) ? "bad" : flags.length ? "warn" : null],
      ["Video", p.video || "—"],
      ["Encoder", p.encoder || "—"],
      ["Audio", p.test_pattern ? "test tone" : p.audio ? (p.mic || "on") : "none (video only)", p.audio ? null : "warn"],
      ["Frame rate", p.fps != null ? `${p.fps.toFixed(1)} fps` : "—"],
      ["Restarts", String(p.restarts ?? 0)],
      ["Last error", p.last_error || "—"],
    ]);
  }

  async function poll() {
    try {
      const res = await fetch("/api/status", { cache: "no-store" });
      const st = await res.json();
      const wasLive = feed.live;
      feed = st.feed || { live: false };
      render(st);
      if (mode === "webrtc") {
        if (!feed.live) {
          teardown();
          clearTimeout(retryTimer);
          showOverlay(waitingText());
        } else if (!connecting && (!pc || !wasLive || feed.session !== session)) {
          // No player yet, the feed just came back, or the Pi reconnected.
          session = feed.session;
          connect();
        }
      } else if (feed.live && !wasLive) {
        hls.src = hlsUrl;   // reload MediaMTX's HLS page when the feed comes back
      }
    } catch (err) {
      setBadge($("badge-feed"), "DASHBOARD DOWN", "bad");
    } finally {
      setTimeout(poll, 2000);
    }
  }

  // Free the server-side session when the tab closes.
  window.addEventListener("pagehide", () => teardown());

  setMode(mode);
  poll();
})();
