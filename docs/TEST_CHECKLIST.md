# astronaut-streamer test checklist

Work top to bottom: each stage builds on the one before. Tick each box when
it passes, and fill in the tables. If something fails, fix it before moving
on; [SETUP.md §10](SETUP.md#10-troubleshooting) covers the usual causes. To
tick boxes as a team, paste this file into a GitHub issue.

Commands are written as run inside `astronaut-streamer/`. From the
billee-hardware-2027 root, use `make astronaut <command>`.

The targets come from [PLAN.md §1](../PLAN.md#acceptance-criteria-proposed):

| # | Target |
|---|---|
| A1 | The ground station sees and hears the feed in Chrome or Firefox: one click for audio |
| A2 | Camera-to-screen latency over the Prism link: < 500 ms |
| A3 | Audio/video offset after 1 h: < ~100 ms |
| A4 | Recovers from Wi-Fi drop, camera replug, or reboot of either box ≤ 10 s after the cause clears, with no keyboard |
| A5 | Boots straight into streaming: no keyboard or monitor needed |
| A6 | No Pi throttling in a 30 min run |

---

## A. Software, on any laptop (no hardware)

- [ ] **A.1** `make test` → ends with `OK`.
- [ ] **A.2** `make lint` → `shellcheck: clean` and `python: compiles`
      (install shellcheck first: `sudo apt install shellcheck`).
- [ ] **A.3** `make e2e` → ends with `21/21 checks passed`: real MediaMTX,
      dashboard, test-pattern sender, and headless Chrome playing WebRTC
      video and audio, including recovery from killed processes.

## B. Jetson install

- [ ] **B.1** JetPack 6: `head -1 /etc/nv_tegra_release` shows `R36`.
- [ ] **B.2** The Prism-side Ethernet has the fixed address:
      `ip -4 addr | grep 10.110.0.230` prints a line. Also with **no DHCP
      server**: plug the Ethernet straight into a laptop, wait 2 minutes,
      and check again (SETUP.md §5).
- [ ] **B.3** The Wi-Fi card can be a hotspot:
      `iw list | grep -A8 "Supported interface modes"` includes `* AP`.
- [ ] **B.4** `make jetson` ends with `Done.` and lists
      `http://10.110.0.230:8080 ... <- ground station (Prism link)`.
- [ ] **B.5** `make status` → `astronaut-mediamtx active`,
      `astronaut-web active`, `Hotspot: UP`.
- [ ] **B.6** Run `make jetson` again → it completes again and says
      `MediaMTX v1.21.3 is already installed` (safe to re-run).
- [ ] **B.7** Reboot the Jetson, touch nothing → `make status` shows the same
      as B.5 (A5).
- [ ] **B.8** Anything else on the Jetson still works: F´ GDS (ports
      5000/5001), spectrometer VNC (5900), ROS2.

## C. Ground-station link

- [ ] **C.1** The laptop on the Prism has a `10.110.0.x` address, and
      `ping 10.110.0.230` answers.
- [ ] **C.2** `http://10.110.0.230:8080` loads: **OFFLINE**, "Waiting for
      astronaut feed", Pi panel "no heartbeat yet".
- [ ] **C.3** `~/.ssh/config` set up (SETUP.md §7) and `ssh jetson` works.

## D. Pi install and bench check

- [ ] **D.1** Card flashed with Raspberry Pi OS Lite 64-bit: hostname, user,
      lab Wi-Fi, Wi-Fi country, SSH on (SETUP.md §6).
- [ ] **D.2** `make pi` ends with `Done.` and said
      `astronaut-sender is running`.
- [ ] **D.3** With the camera and the USB speaker both plugged in, `make probe`
      ends with `0 problem(s)`. The mic line says
      `same USB device as the camera`, the speaker line names the USB
      speaker, and you **heard** the beep.
- [ ] **D.4** Run `make probe` again **on the battery pack** → no throttle
      flags (in particular no under-voltage).

| Bench numbers (from `make probe`) | Result |
|---|---|
| Camera and selected mode | |
| Mic peak dB while talking | |
| `h264_v4l2m2m` on test pattern: speed / CPU cores | |
| `libx264` on test pattern: speed / CPU cores | |
| Live camera: encoder / speed / CPU cores | |
| CPU temperature after the run | |

## E. Hotspot link

- [ ] **E.1** The Pi joins the hotspot: reboot it near the Jetson (or
      `sudo nmcli connection up astro-link`).
- [ ] **E.2** On the Jetson, `make hotspot` lists the Pi under "Connected
      devices" and shows `10.42.0.2`.
- [ ] **E.3** From the ground station, `ssh astronaut-pi` works (through the
      Jetson).
- [ ] **E.4** Throughput: install `iperf3` on both. Run `iperf3 -s` on the
      Jetson and `iperf3 -c 10.42.0.1 -t 10` on the Pi. You want at least 3×
      the stream bitrate (≥ 6 Mbit/s for the default 2 Mbit/s).

| Distance / conditions | iperf3 Mbit/s | Signal dBm (Pi panel) |
|---|---|---|
| 5 m, line of sight | | |
| 20 m, line of sight | | |
| 50 m, line of sight | | |
| Through an obstacle (wall, rover body) | | |

## F. Test pattern, end to end

- [ ] **F.1** On the Pi, `make test-pattern` → within a few seconds the
      dashboard says **LIVE**, shows colour bars, and plays a 440 Hz tone after
      **🔊 Click for audio**.
- [ ] **F.2** Ctrl-C → it prints `Starting astronaut-sender again` → the
      dashboard switches back to the camera within ~10 s.

## G. Real camera

- [ ] **G.1** Dashboard **LIVE** with the camera image. Stream panel: Tracks
      `H264 + Opus`, no **NO AUDIO** badge.
- [ ] **G.2** Click for audio → you hear the room (A1).
- [ ] **G.3** Latency (A2). Point the camera at a stopwatch on a phone placed
      next to the ground-station screen, and photograph both together. The
      difference between the two clocks is the latency. Take 3 readings.
- [ ] **G.4** A/V sync (A3, first reading). Clap in front of the camera: the
      sound and the hands meeting should line up.
- [ ] **G.5** Pi panel: Status `streaming`, frame rate ≈ 30 fps, an encoder
      shown, signal and temperature reasonable.
- [ ] **G.6** **Compatibility (HLS)** plays (2-4 s behind), and
      **Low latency (WebRTC)** comes back.
- [ ] **G.7** A second viewer (another laptop or tab) also plays, and the
      first keeps playing.
- [ ] **G.8** *(optional, needs a monitor and headset on the Jetson)*
      `make view` plays with sound.

| Latency reading | 1 | 2 | 3 |
|---|---|---|---|
| ms | | | |

## H. Failure drills (A4)

Do each one with the dashboard open at the ground station and **without
touching any keyboard** to recover. Time from the moment the cause is removed
until the picture is back.

| # | Do this | Expect | Back in (s) | Pass |
|---|---|---|---|---|
| H.1 | Unplug the camera 10 s, plug it back | NO CAMERA / OFFLINE, then LIVE | | [ ] |
| H.2 | Unplug the USB speaker | No effect on the stream | — | [ ] |
| H.3 | On the Pi: `MIC_MATCH=nothing` in `astronaut.local.env`, `make restart` | Video continues, **NO AUDIO** badge. Remove the line, `make restart`: audio back | | [ ] |
| H.4 | On the Pi: `sudo systemctl kill astronaut-sender` | systemd restarts it; LIVE again | | [ ] |
| H.5 | On the Pi: `pkill ffmpeg` | The sender restarts ffmpeg; LIVE again | | [ ] |
| H.6 | Reboot the Pi | LIVE again after boot | | [ ] |
| H.7 | Reboot the Jetson | Hotspot, services, Pi and page all come back | | [ ] |
| H.8 | Both off. Power the Pi first, the Jetson 2 min later. Then the reverse | LIVE either way | | [ ] |
| H.9 | On the Jetson: `sudo bash jetson/hotspot.sh stop`, wait 30 s, `... start` | Pi rejoins; LIVE again | | [ ] |
| H.10 | Walk the Pi out of Wi-Fi range and back | PI SILENT / OFFLINE, then LIVE | | [ ] |
| H.11 | Unplug the Prism link 30 s, plug it back | DASHBOARD DOWN, then the page recovers by itself | | [ ] |
| H.12 | On the Jetson: `sudo systemctl restart astronaut-mediamtx` | SERVER DOWN briefly, then LIVE | | [ ] |
| H.13 | On the Jetson: `sudo systemctl restart astronaut-web` | The page recovers by itself | | [ ] |
| H.14 | Close the browser tab and open the dashboard again | Plays; one click for audio | | [ ] |

## I. Rover integration

- [ ] **I.1** With the full ROS2 stack running on the Jetson, the stream is
      unaffected. DDS is limited to the Prism-side interface (SETUP.md §5).
- [ ] **I.2** F´ GDS and the spectrometer VNC work while streaming.
- [ ] **I.3** Jetson load while streaming (`top`): `mediamtx` and `python3`
      should use little CPU (they don't re-encode).

## J. Two-hour soak

- [ ] **J.1** At the start, record the values below.
- [ ] **J.2** After 2 hours: still **LIVE**; restarts unchanged (or each one
      explained in the logs); the clap test still in sync (A3); memory roughly
      flat; no throttling (A6).

| | Start | +2 h |
|---|---|---|
| Pi panel: Restarts | | |
| Pi: CPU temperature / throttling | | |
| Pi: `ps -o rss= -C ffmpeg` (KB) | | |
| Jetson: `ps -o rss= -C mediamtx` (KB) | | |
| Jetson: `ps -o rss= -p $(systemctl show -p MainPID --value astronaut-web)` (KB) | | |
| Clap test in sync? | | |

## K. Field

- [ ] **K.1** Prism link at competition distance with normal rover traffic:
      **LIVE**, steady bitrate.
- [ ] **K.2** Walk test: how far can the astronaut go from the rover with the
      picture still usable?
- [ ] **K.3** If it stutters at range, tune and re-test: `VIDEO_BITRATE=1M`,
      a lower resolution (`VIDEO_WIDTH=960`, `VIDEO_HEIGHT=540`), or
      `TRANSPORT=srt` in the Pi's `astronaut.local.env`, then `make restart`.

| Walk test | Distance (m) | Signal (dBm) | Picture |
|---|---|---|---|
| Start of stutter | | | |
| Picture lost | | | |

## Sign-off

| | |
|---|---|
| Date / tester | |
| astronaut-streamer commit (`git rev-parse --short HEAD`) | |
| Resolution / fps / bitrate | |
| Encoder (Pi panel) / transport | |
| Notes | |
