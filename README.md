# astronaut-streamer

Live video and audio from a USB camera on a **Raspberry Pi 4B** (the
astronaut) to a **Jetson Orin** (the rover) over the Jetson's Wi-Fi hotspot.
The Jetson forwards the feed to a browser at the **ground station** over the
Ubiquiti Prism link, at **`http://10.110.0.230:8080`**.

Part of BILLEE ([billee-hardware-2027](https://github.com/BroncoSpace-BILLEE/billee-hardware-2027)).
The full design (architecture, ports, phases, failure modes, decisions) is
in [PLAN.md](PLAN.md).

```
USB camera + mic ─► Pi 4B ──Wi-Fi hotspot──► Jetson Orin ──Prism link──► ground-station browser
                    (ffmpeg)  10.42.0.2 → .1  (MediaMTX + Flask)  10.110.0.230:8080
```

## Status

Phase 0 (bench check) is ready. Streaming itself comes next; see the phases
in [PLAN.md §6](PLAN.md#6-phases).

| Piece | Runs on | State |
|---|---|---|
| `jetson/hotspot.sh` — Wi-Fi hotspot for the Pi | Jetson | ready |
| `pi/pi_install_me.sh` — packages, SSH, joins the hotspot | Pi | ready |
| `pi/probe_devices.py` — camera, mic, speaker, encoder, Wi-Fi check | Pi | ready |
| MediaMTX, `pi/sender.py`, Flask dashboard | both | next |

## Phase 0: bring-up

**Jetson** (JetPack 6):

```bash
sudo ./jetson/hotspot.sh up       # asks for the Wi-Fi password; starts at every boot after this
./jetson/hotspot.sh status        # hotspot state and connected devices
```

**Pi** (Raspberry Pi OS Lite 64-bit, Bookworm), while it still has internet:

```bash
sudo ./pi/pi_install_me.sh        # packages, SSH, saves the hotspot (Pi = 10.42.0.2)
./pi/probe_devices.py             # camera modes, mic level, speaker, H.264 encoders, Wi-Fi
./pi/probe_devices.py --beep      # also play a test tone on the USB speaker
sudo nmcli connection up astro-link   # switch to the hotspot now (it joins by itself at boot)
```

To skip the password prompt, put `HOTSPOT_PSK=<password>` in
`config/astronaut.local.env` (git-ignored) on each machine.

## Reaching the Pi from the ground station

The Pi is on the hotspot behind the Jetson, so SSH goes through the Jetson.
In `~/.ssh/config` on the ground-station laptop:

```
Host jetson
    HostName 10.110.0.230
    User <jetson-user>

Host astronaut-pi
    HostName 10.42.0.2
    User <pi-user>
    ProxyJump jetson
```

Then `ssh astronaut-pi`. Run `ssh-copy-id jetson` and `ssh-copy-id astronaut-pi`
once to stop the password prompts.

## Settings

Everything lives in [`config/astronaut.env`](config/astronaut.env): hotspot
SSID/channel and addresses, the Jetson's ground-side IP, resolution, frame
rate, bitrates, and optional `CAMERA_MATCH` / `MIC_MATCH` / `SPEAKER_MATCH` if
auto-detection picks the wrong device. `config/astronaut.local.env` overrides
it per machine, and environment variables override both:

```bash
VIDEO_WIDTH=640 VIDEO_HEIGHT=480 ./pi/probe_devices.py
```

## Layout

```
config/   astronaut.env (settings), config.sh (shell helpers)
jetson/   hotspot.sh
pi/       pi_install_me.sh, probe_devices.py, devices.py (device discovery library)
tests/    unit tests (standard library only)
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```
