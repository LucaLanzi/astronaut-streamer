# astronaut-streamer

Live video and audio from a USB camera on a **Raspberry Pi 4B** (the
astronaut) to a **Jetson Orin** (the rover) over the Jetson's Wi-Fi hotspot.
The Jetson forwards the feed to a browser at the **ground station** over the
Ubiquiti Prism link, at **`http://10.110.0.230:8080`**.

Part of BILLEE ([billee-hardware-2027](https://github.com/BroncoSpace-BILLEE/billee-hardware-2027),
where it's the `astronaut-streamer` submodule).

```
USB camera + mic ─► Pi 4B ──Wi-Fi hotspot──► Jetson Orin ──Prism link──► ground-station browser
                    sender     10.42.0.2 → .1  MediaMTX + dashboard  10.110.0.230:8080
```

- **Pi:** `pi/sender.py` runs ffmpeg (H.264 + Opus) pushing one RTSP stream
  to the Jetson. It waits for the Jetson, survives camera replugs and Wi-Fi
  drops, falls back to software encoding if the hardware encoder fails, sends
  video only if the mic fails, and reports the Pi's health.
- **Jetson:** [MediaMTX](https://github.com/bluenviron/mediamtx) turns the
  stream into WebRTC (low latency) and HLS (fallback) without re-encoding.
  `jetson/app.py` serves the dashboard: player, one-click audio, stream and Pi
  status.
- **Ground station:** any Chrome or Firefox; no plugins, no internet.

## Quick start

```bash
make            # list the commands
make e2e        # optional: run the whole pipeline on your laptop first (21 checks)
make jetson     # on the Jetson: everything, hotspot included
make pi         # on the Pi: everything, sender service included
make probe      # on the Pi: bench check (camera, mic, speaker, encoders, Wi-Fi)
make status     # on either: what's running
make push-pi    # on your laptop: send your current commit to the Pi through the Jetson
```

From billee-hardware-2027's root, use `make astronaut <command>`.

**Full guide, from flashing the Pi to day-to-day use and troubleshooting:
[docs/SETUP.md](docs/SETUP.md).** To prove it works, step by step:
[docs/TEST_CHECKLIST.md](docs/TEST_CHECKLIST.md). Design and decisions:
[PLAN.md](PLAN.md).

## Layout

```
Makefile               all commands (`make` lists them)
config/astronaut.env   settings shared by both machines (overrides: astronaut.local.env)
config/config.sh       shell helpers: settings, templates, MediaMTX download + checksum
common/envconfig.py    the same settings loader for Python
pi/                    sender.py, devices.py, probe_devices.py, pi_install_me.sh, service
jetson/                app.py + templates/static (dashboard), mediamtx.yml.in,
                       jetson_install_me.sh, hotspot.sh, local_view.sh, services
tools/                 status.sh, uninstall.sh, e2e_local.py + e2e_setup.sh
tests/                 unit tests (standard library; dashboard tests need Flask)
docs/                  SETUP.md, TEST_CHECKLIST.md
```

## Development

```bash
make test       # unit tests: ffmpeg command, failure handling, supervisor
                # (with a fake ffmpeg), dashboard, shell/Python config parity
make lint       # shellcheck + Python compile check
make e2e        # real MediaMTX + dashboard + test-pattern sender, checked in
                # headless Chrome, including recovery from failures
```

Tested on Python 3.10 with Flask 2.0.1 and waitress 1.4.4 (what the Jetson's
Ubuntu 22.04 ships), and on current Python and Flask.

To work on it, push to `main` here, then update the submodule pointer on
billee-hardware-2027's `lucadev` branch.
