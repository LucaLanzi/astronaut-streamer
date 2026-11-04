// Astronaut feed dashboard: plays the stream from MediaMTX over WebRTC
// (WHEP), with MediaMTX's own HLS page as a fallback, shows stream and Pi
// status from /api/status, and sends the ground station's microphone to the
// Pi (talk-back, WHIP). Every request goes to this page's own server, which
// passes it on to MediaMTX, so an HTTPS page never makes an insecure one.
// No external scripts: the ground station may have no internet.
"use strict";

(() => {
  const cfg = JSON.parse(document.getElementById("page-config").textContent);
  const whepUrl = `/webrtc/${cfg.path}/whep`;
  const whipUrl = `/webrtc/${cfg.talkbackPath}/whip`;
  const hlsUrl = `/hls/${cfg.path}/`;

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
  window.astro = {
    get pc() { return pc; },
    get connects() { return connects; },
    get talkPc() { return talk.pc; },
    connect: () => connect(),
  };

  function loadPref(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }
  function savePref(key, value) {
    try { localStorage.setItem(key, value); } catch { /* private mode: fine */ }
  }
  function loadMode() { return loadPref("astro-mode") === "hls" ? "hls" : "webrtc"; }
  function saveMode(m) { savePref("astro-mode", m); }

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

  // --------------------------------------------------------------- talk-back
  // The ground station's mic goes to MediaMTX over WebRTC (WHIP) and the Pi's
  // talkback.py plays it on the headphones. The connection stays up while
  // talk-back is on; Mute and push-to-talk only switch the mic track, so they
  // act instantly.

  const talk = { pc: null, stream: null, starting: false, muted: false, ptt: false,
                 meter: null, live: false, listeners: 0 };
  const micSelect = $("tb-mic");
  const startBtn = $("tb-start");
  const muteBtn = $("tb-mute");
  const tbStatus = $("tb-status");

  function talkbackAvailable() {
    return window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia;
  }

  function setTbStatus(text, level) {
    tbStatus.textContent = text;
    tbStatus.className = `note ${level || ""}`;
  }

  // Fill the dropdown. Until the browser has been allowed to use the mic it
  // hides the devices' names, so then there's just "Default microphone"; the
  // full list appears after talk-back is switched on the first time.
  async function listMics(selected) {
    const inputs = (await navigator.mediaDevices.enumerateDevices())
      .filter((d) => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default");
    const want = selected !== undefined ? selected : (micSelect.value || loadPref("astro-mic") || "");
    const options = [["", "Default microphone"], ...inputs.map((d, i) => [d.deviceId, d.label || `Microphone ${i + 1}`])];
    micSelect.replaceChildren(...options.map(([value, label]) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      return opt;
    }));
    micSelect.value = options.some(([value]) => value === want) ? want : "";
  }

  function micConstraints() {
    return { audio: { deviceId: micSelect.value ? { exact: micSelect.value } : undefined,
                      echoCancellation: true, noiseSuppression: true, autoGainControl: true,
                      channelCount: 1 } };
  }

  function startMeter(stream) {
    stopMeter();
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    const bar = $("tb-level");
    const tick = () => {
      analyser.getByteTimeDomainData(data);
      let sum = 0;
      for (const v of data) sum += ((v - 128) / 128) ** 2;
      const rms = Math.sqrt(sum / data.length);
      bar.style.width = `${Math.min(100, Math.round(rms * 400))}%`;
      talk.meter.raf = requestAnimationFrame(tick);
    };
    talk.meter = { ctx, raf: requestAnimationFrame(tick) };
  }

  function stopMeter() {
    if (!talk.meter) return;
    cancelAnimationFrame(talk.meter.raf);
    talk.meter.ctx.close().catch(() => {});
    talk.meter = null;
    $("tb-level").style.width = "0";
  }

  function setMicEnabled(on) {
    if (talk.stream) talk.stream.getAudioTracks().forEach((t) => { t.enabled = on; });
    $("badge-talk").hidden = !(talk.pc && on);
  }

  function updateTalkUi() {
    const on = Boolean(talk.pc);
    startBtn.textContent = on ? "■ Talk-back: ON" : "🎙 Talk-back: OFF";
    startBtn.className = on ? "stop" : "primary";
    muteBtn.hidden = !on;
    muteBtn.textContent = talk.muted ? "Unmute" : "Mute";
    muteBtn.className = talk.muted ? "muted" : "";
    setMicEnabled(on && (!talk.muted || talk.ptt));
    if (!on) {
      if (!talk.starting) setTbStatus("Off. Pick a microphone and switch talk-back on (the browser asks for permission the first time).");
      return;
    }
    const listening = talk.live && talk.listeners > 0;
    const who = listening ? "The Pi is listening." : "Waiting for the Pi to start playing…";
    if (talk.muted && !talk.ptt) setTbStatus(`Muted: hold Space to talk. ${who}`, listening ? "" : "warn");
    else setTbStatus(listening ? "Live: the astronaut hears you." : `Live. ${who}`, listening ? "live" : "warn");
  }

  async function startTalk() {
    if (talk.pc || talk.starting) return;
    talk.starting = true;
    setTbStatus("Connecting…");
    try {
      talk.stream = await navigator.mediaDevices.getUserMedia(micConstraints());
      savePref("astro-mic", micSelect.value);
      // Now the browser shows the microphones' names: refresh the dropdown.
      listMics(micSelect.value).catch(() => {});
      const peer = new RTCPeerConnection();
      talk.stream.getAudioTracks().forEach((t) => peer.addTransceiver(t, { direction: "sendonly" }));
      peer.onconnectionstatechange = () => {
        if (peer === talk.pc && peer.connectionState === "failed") stopTalk("Connection to the Jetson lost.");
      };
      await peer.setLocalDescription(await peer.createOffer());
      await waitForIce(peer, 1500);
      const res = await fetch(whipUrl, {
        method: "POST",
        headers: { "Content-Type": "application/sdp" },
        body: peer.localDescription.sdp,
      });
      if (res.status !== 201) throw new Error(`media server answered ${res.status}`);
      await peer.setRemoteDescription({ type: "answer", sdp: await res.text() });
      talk.pc = peer;
      talk.muted = false;
      startMeter(talk.stream);
    } catch (err) {
      stopTalk(`Couldn't start talk-back: ${err.message || err}`);
    } finally {
      talk.starting = false;
      updateTalkUi();
    }
  }

  function stopTalk(message) {
    if (talk.pc) talk.pc.close();
    talk.pc = null;
    if (talk.stream) talk.stream.getTracks().forEach((t) => t.stop());
    talk.stream = null;
    talk.ptt = false;
    stopMeter();
    updateTalkUi();
    if (message) setTbStatus(message, "warn");
  }

  // Switch microphones without reconnecting.
  async function changeMic() {
    savePref("astro-mic", micSelect.value);
    if (!talk.pc) return;
    try {
      const fresh = await navigator.mediaDevices.getUserMedia(micConstraints());
      const track = fresh.getAudioTracks()[0];
      const sender = talk.pc.getSenders().find((s) => s.track && s.track.kind === "audio");
      if (sender) await sender.replaceTrack(track);
      if (talk.stream) talk.stream.getTracks().forEach((t) => t.stop());
      talk.stream = fresh;
      startMeter(fresh);
      updateTalkUi();
    } catch (err) {
      setTbStatus(`Couldn't switch microphone: ${err.message || err}`, "warn");
    }
  }

  function isTyping(ev) {
    const t = ev.target;
    return t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA");
  }

  startBtn.addEventListener("click", () => (talk.pc ? stopTalk() : startTalk()));
  muteBtn.addEventListener("click", () => { talk.muted = !talk.muted; updateTalkUi(); });
  micSelect.addEventListener("change", changeMic);
  // Push-to-talk: hold Space while muted.
  window.addEventListener("keydown", (ev) => {
    if (ev.code !== "Space" || ev.repeat || isTyping(ev) || !talk.pc || !talk.muted) return;
    ev.preventDefault();
    talk.ptt = true;
    updateTalkUi();
  });
  window.addEventListener("keyup", (ev) => {
    if (ev.code !== "Space" || !talk.ptt) return;
    ev.preventDefault();
    talk.ptt = false;
    updateTalkUi();
  });

  function initTalkback() {
    if (!talkbackAvailable()) {
      const link = $("tb-secure-link");
      link.href = `https://${window.location.hostname}:${cfg.tlsPort}/`;
      link.textContent = link.href;
      $("tb-insecure").hidden = false;
      return;
    }
    $("tb-controls").hidden = false;
    updateTalkUi();
    listMics().catch(() => {});
    navigator.mediaDevices.addEventListener("devicechange", () => listMics().catch(() => {}));
  }

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
    const tb = st.talkback || {};
    talk.live = Boolean(tb.live);
    talk.listeners = tb.listeners || 0;
    if (talk.pc) updateTalkUi();
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
      ["Talk-back", tb.live ? (tb.listeners > 0 ? "live, the Pi is listening" : "live, the Pi isn't playing it yet")
        : "off", tb.live && !(tb.listeners > 0) ? "warn" : null],
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

  // Free the server-side sessions when the tab closes.
  window.addEventListener("pagehide", () => { teardown(); stopTalk(); });

  setMode(mode);
  initTalkback();
  poll();
})();
