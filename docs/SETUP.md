# astronaut-streamer: setup and operation, top to bottom

Live video and audio from a USB camera on the astronaut's Raspberry Pi, to the
rover's Jetson over the Jetson's Wi-Fi hotspot, then to a browser at the
ground station over the Ubiquiti Prism link.

```
 USB camera + mic          Wi-Fi hotspot "ASTRO-LINK"            Prism point-to-point
 USB speaker                  2.4 GHz, WPA2                         link (bridged)
      │                                                                    │
┌─────┴──────────┐  RTSP   ┌───────────────────────────┐   WebRTC   ┌──────┴──────────┐
│ Raspberry Pi 4B│ ──────► │ Jetson Orin (JetPack 6)   │ ─────────► │ Ground-station  │
│ 10.42.0.2      │ H.264 + │ 10.42.0.1 (hotspot)       │  + HTTP    │ laptop, browser │
│ sender service │  Opus   │ 10.110.0.230 (Prism side) │            │ 10.110.0.x      │
└────────────────┘         │ MediaMTX + dashboard :8080│            └─────────────────┘
                           └───────────────────────────┘
```

The design and its reasoning are in [PLAN.md](../PLAN.md). To prove it all
works, follow [TEST_CHECKLIST.md](TEST_CHECKLIST.md).

## Contents

1. [What you need](#1-what-you-need)
2. [Get the code](#2-get-the-code)
3. [Settings](#3-settings)
4. [Try it on a laptop first (optional)](#4-try-it-on-a-laptop-first-optional)
5. [Jetson](#5-jetson)
6. [Raspberry Pi](#6-raspberry-pi)
7. [Ground station](#7-ground-station)
8. [Day-to-day operation](#8-day-to-day-operation)
9. [Command reference](#9-command-reference)
10. [Troubleshooting](#10-troubleshooting)
11. [Uninstall](#11-uninstall)

## 1. What you need

| Item | Notes |
|---|---|
| Raspberry Pi 4B + microSD (16 GB+) | Raspberry Pi OS Lite 64-bit (Bookworm) |
| USB camera with a built-in mic | Must offer MJPEG at 1280x720 @ 30 fps (most UVC webcams do) |
| USB speaker | Plugged into the Pi; not used by the one-way stream yet ([PLAN.md §3.6](../PLAN.md#36-usb-speaker-on-the-pi)) |
| Portable battery for the Pi | 5 V / 3 A. The dashboard shows under-voltage if it sags |
| Jetson Orin (AGX or Nano) | JetPack 6 (Ubuntu 22.04), working Wi-Fi card, Ethernet to the rover-end Prism |
| Ubiquiti Prism pair | Bridge mode: the ground station and the Jetson's `10.110.0.230` share one subnet |
| Ground-station laptop | Chrome or Firefox. No plugins and no internet needed |
| Internet, once | Each installer downloads packages the first time |

## 2. Get the code

astronaut-streamer is its own repo
([LucaLanzi/astronaut-streamer](https://github.com/LucaLanzi/astronaut-streamer)),
included in billee-hardware-2027 as a submodule. Either way of getting it works:

**Through billee-hardware-2027** (the Jetson already has it):

```bash
cd billee-hardware-2027
git checkout lucadev && git pull
make astronaut            # fetches the submodule if needed, then lists its commands
```

From the billee-hardware-2027 root, every command is `make astronaut <command>`.

**Just this repo** (lighter, good for the Pi):

```bash
sudo apt install -y git make
git clone https://github.com/LucaLanzi/astronaut-streamer.git
cd astronaut-streamer
make                      # lists the commands
```

Inside `astronaut-streamer/`, every command is `make <command>`. The rest of
this guide uses that short form.

## 3. Settings

Everything is in [`config/astronaut.env`](../config/astronaut.env), and the
defaults match the rover:

| Setting | Default | Meaning |
|---|---|---|
| `HOTSPOT_SSID` / `HOTSPOT_PSK` | `ASTRO-LINK` / *(asked)* | Hotspot name and password |
| `HOTSPOT_BAND` / `HOTSPOT_CHANNEL` | `bg` / `6` | 2.4 GHz channel 6 |
| `JETSON_HOTSPOT_IP` / `PI_HOTSPOT_IP` | `10.42.0.1` / `10.42.0.2` | Hotspot addresses |
| `JETSON_GROUND_IP` | `10.110.0.230` | The Jetson's address on the Prism side |
| `VIDEO_WIDTH` / `VIDEO_HEIGHT` / `VIDEO_FPS` | `1280` / `720` / `30` | Camera mode (nearest available is used) |
| `VIDEO_BITRATE` / `AUDIO_BITRATE` | `2M` / `32k` | Stream bitrates |
| `VIDEO_ENCODER` | `auto` | Pi hardware encoder if it works, else `libx264` |
| `TRANSPORT` | `rtsp` | `srt` for a lossy link |
| `WEB_PORT` | `8080` | Dashboard port |
| `CAMERA_MATCH` / `MIC_MATCH` / `SPEAKER_MATCH` | *(empty)* | Only if auto-detection picks the wrong device |

Don't edit `astronaut.env` for one machine. Put overrides in
`config/astronaut.local.env` (git-ignored, same `KEY=value` format). Create it
on **both** the Jetson and the Pi with the same hotspot password, so the
installers don't ask:

```bash
echo 'HOTSPOT_PSK=choose-a-long-password' > config/astronaut.local.env
```

Settings used on both sides (`RTSP_PORT`, `WEB_PORT`, `STREAM_PATH`, the IPs)
must match on both machines.

## 4. Try it on a laptop first (optional)

Any Linux laptop can run the whole pipeline (media server, dashboard, and a
test-pattern sender), then open the dashboard in headless Chrome and check
that video and audio really play, including recovery from failures:

```bash
make test     # unit tests, a few seconds, no setup
make e2e      # full pipeline; first run downloads MediaMTX + a Python env (no sudo)
```

`make e2e` should end with `21/21 checks passed`. It uses ports 18000-19999,
so it never collides with a real install.

## 5. Jetson

**Before you start:** JetPack 6 is installed, and the Jetson's Ethernet already
has its fixed address `10.110.0.230` on the rover network (check with
`ip -4 addr`; set it in NetworkManager if not, with the netmask and gateway
your rover network uses).

```bash
make jetson
```

It asks for your password (sudo) and, if not in `astronaut.local.env`, the
hotspot password. In about 2 minutes it:

1. installs the packages (ffmpeg, Flask, waitress, NetworkManager pieces);
2. downloads the pinned MediaMTX and checks its SHA-256. It keeps a copy in
   `vendor/`, so later re-installs work offline;
3. finds the Wi-Fi and Prism-side interfaces (names differ between Jetson
   models);
4. refuses to continue if another program already uses one of its ports, and
   says which;
5. writes `/etc/astronaut-streamer/mediamtx.yml` and installs two services,
   `astronaut-mediamtx` and `astronaut-web`, which start at every boot;
6. opens the ports in `ufw` if the firewall is on;
7. checks both services answer, and prints the dashboard addresses;
8. creates the hotspot `ASTRO-LINK` (WPA2, 2.4 GHz ch 6, `10.42.0.1`),
   which also comes up at every boot.

Check it:

```bash
make status    # services, hotspot, connected devices, stream state
```

From any machine on the rover network, `http://10.110.0.230:8080` shows the
dashboard with **OFFLINE: Waiting for astronaut feed**.

**ROS2 on the same Jetson:** keep DDS traffic off the hotspot. Wi-Fi sends
multicast at its slowest rate, so DDS discovery can eat the astronaut link's
airtime. For CycloneDDS:

```xml
<!-- export CYCLONEDDS_URI=file:///path/to/cyclonedds.xml -->
<CycloneDDS><Domain><General><Interfaces>
  <NetworkInterface name="eth0"/>   <!-- the Prism-side interface -->
</Interfaces></General></Domain></CycloneDDS>
```

For Fast DDS (the Humble default), use an XML profile with an interface
whitelist for the Prism-side address.

Re-run `make jetson` any time. It's also how changes to the settings take
effect on the Jetson.

## 6. Raspberry Pi

**Flash the card** with Raspberry Pi Imager: *Raspberry Pi OS Lite (64-bit)*.
In the customisation settings:
- hostname `astronaut-pi`
- a username and password
- **your lab/home Wi-Fi** (the Pi needs internet for the install)
- Wi-Fi country
- **enable SSH**

Boot it and SSH in over the lab network.

```bash
sudo apt install -y git make
git clone https://github.com/LucaLanzi/astronaut-streamer.git
cd astronaut-streamer
echo 'HOTSPOT_PSK=the-same-password-as-the-jetson' > config/astronaut.local.env
make pi
```

`make pi`:

1. sets the Wi-Fi country (Pi OS keeps Wi-Fi off until it's set);
2. installs ffmpeg, v4l-utils, alsa-utils, iw, and the SSH server, and turns
   SSH on;
3. adds you to the `video` and `audio` groups;
4. saves the hotspot as the Pi's preferred Wi-Fi, with the fixed address
   `10.42.0.2` and power saving off;
5. runs a quick bench check (camera, mic, speaker detection);
6. installs and starts the `astronaut-sender` service. It waits for the
   Jetson, then streams on its own, at every boot.

Now run the full bench check and keep its summary (the checklist asks for the
numbers):

```bash
make probe     # camera modes, mic level, speaker beep, encoder speed + CPU, Wi-Fi
```

**Switch to the hotspot.** Take the Pi near the Jetson and either reboot it,
or:

```bash
sudo nmcli connection up astro-link   # drops this SSH session if it's over Wi-Fi
```

Within a few seconds the dashboard turns **LIVE**. From now on, reach the Pi
through the Jetson (next section).

## 7. Ground station

1. Plug the laptop into the ground-end Prism, and give it an address on the
   rover subnet (`10.110.0.x`). `ping 10.110.0.230` must answer.
2. Open **`http://10.110.0.230:8080`** in Chrome or Firefox.
3. The video starts on its own, muted. Click **🔊 Click for audio** once
   (browsers require a click before playing sound).

**SSH to the Jetson and the Pi** from the laptop. Add to `~/.ssh/config`:

```
Host jetson
    HostName 10.110.0.230
    User <jetson-user>

Host astronaut-pi
    HostName 10.42.0.2
    User <pi-user>
    ProxyJump jetson
```

Then `ssh jetson` and `ssh astronaut-pi`. Run `ssh-copy-id jetson` and
`ssh-copy-id astronaut-pi` once to stop the password prompts.

**OBS (optional)** for overlays or restreaming: add a *Media Source* with input
`rtsp://10.110.0.230:8554/astronaut`.

## 8. Day-to-day operation

**Power-up order doesn't matter.** The Jetson brings up the hotspot, MediaMTX
and the dashboard at boot. The Pi joins the hotspot when it sees it, and the
sender starts streaming as soon as the Jetson answers. The ground-station page
reconnects by itself, so just keep it open.

**Reading the dashboard:**

| You see | Meaning |
|---|---|
| **LIVE** | Stream is up |
| **OFFLINE**, "Waiting for astronaut feed" | MediaMTX is fine but the Pi isn't publishing (off, out of range, or no camera) |
| **SERVER DOWN** | MediaMTX on the Jetson isn't answering |
| **DASHBOARD DOWN** | The page can't reach the Jetson at all (Prism link down?) |
| **NO AUDIO** | Streaming video only: the mic is missing or failed |
| **PI SILENT** | No heartbeat from the Pi for 6 s or more |
| Pi panel: Wi-Fi signal | dBm: about -60 good, -70 fair, -80 weak |
| Pi panel: Throttling | Under-voltage or overheating on the Pi |
| Pi panel: Encoder | `h264_v4l2m2m` = hardware; `libx264` = software (more CPU and heat) |

If WebRTC won't play but the badge says LIVE, click **Compatibility (HLS)**.
It works over any network that carries the page, with 2-4 s of delay.

**Changing settings:** edit `config/astronaut.local.env` on the machine
concerned, then:
- **Pi** (resolution, bitrate, encoder, transport, devices): `make restart`
  on the Pi.
- **Jetson** (ports, interfaces, MediaMTX): `make jetson` again.
- **Hotspot** (SSID, password, channel): `sudo bash jetson/hotspot.sh up` on
  the Jetson, then `make pi` on the Pi.

**Testing without the camera:** `make test-pattern` on the Pi sends colour bars
and a 440 Hz tone. It pauses the service and starts it again when you press
Ctrl-C.

**Watching on the Jetson itself** (bench, with a monitor and headset):
`make view`.

## 9. Command reference

| Command | Where | What it does |
|---|---|---|
| `make jetson` | Jetson | Install or update everything, hotspot included |
| `make hotspot` | Jetson | Hotspot state and connected devices |
| `sudo bash jetson/hotspot.sh up\|start\|stop\|remove` | Jetson | Change the hotspot |
| `make view` | Jetson | Watch and listen locally (ffplay) |
| `make pi` | Pi | Install or update everything, sender service included |
| `make probe` | Pi | Full bench check (pauses the sender) |
| `make test-pattern` | Pi | Colour bars + tone instead of the camera (pauses the sender) |
| `make status` | either | Services, Wi-Fi/hotspot, and stream state |
| `make logs` | either | Follow the service logs |
| `make restart` | either | Restart this machine's services |
| `make uninstall` | either | Remove this machine's services |
| `make test` | any | Unit tests |
| `make e2e` | laptop | Whole pipeline locally, checked in headless Chrome |
| `make lint` | any | shellcheck + Python compile check |

From billee-hardware-2027's root, prefix each with `astronaut`, for example
`make astronaut status`.

## 10. Troubleshooting

Start with `make status` on the machine in question. `make logs` shows what
the services are doing.

| Symptom | Check | Fix |
|---|---|---|
| Dashboard won't load at the ground station | `ping 10.110.0.230`; on the Jetson, `make status` | Prism link, laptop address (`10.110.0.x`), radios in bridge mode; `ufw status` |
| `make jetson` stops with "port … is used by …" | The program it names | Stop that program, or pick another port in `astronaut.local.env` |
| SERVER DOWN | `journalctl -u astronaut-mediamtx -n 50` | `make restart`; re-run `make jetson` |
| OFFLINE, Pi panel empty or PI SILENT | Jetson: `make hotspot` lists connected devices | Pi off, out of range, or not on the hotspot (password mismatch?); on the Pi: `sudo nmcli connection up astro-link` |
| Pi panel: "waiting for the Jetson" | On the Pi: `timeout 2 bash -c '</dev/tcp/10.42.0.1/8554' && echo open` | MediaMTX down, or a firewall on the Jetson |
| Pi panel: NO CAMERA | On the Pi: `v4l2-ctl --list-devices` | Replug; set `CAMERA_MATCH` if there are several cameras |
| NO AUDIO | `make probe` on the Pi | Mic missing or muted (`alsamixer`); "device busy": `sudo fuser -v /dev/snd/*`; set `MIC_MATCH` |
| LIVE, but black video in WebRTC | Try **Compatibility (HLS)** | If HLS plays, UDP port 8189 isn't getting through: check the Prism bridge or a firewall |
| No sound after clicking | Tab muted? System volume? | Firefox: allow autoplay for the site |
| Video stutters or freezes | Pi panel: Wi-Fi signal, Throttling | Move closer or improve antennas; lower `VIDEO_BITRATE` or the resolution; try `TRANSPORT=srt` |
| Pi hot, Encoder says `libx264` | `make probe` encoder section | Lower the resolution, add a heatsink, check why the hardware encoder failed (`make logs`) |
| Delay keeps growing | Pi panel bitrate, Wi-Fi signal | `TRANSPORT=srt` in the Pi's `astronaut.local.env`, then `make restart` |
| Hotspot won't start | `iw list` shows `* AP` under "Supported interface modes" | No AP support: USB Wi-Fi adapter (PLAN.md §8); `sudo apt install dnsmasq-base` |
| Pi never joins | On the Pi: `nmcli device wifi list` shows ASTRO-LINK? | Same password on both; Wi-Fi country set; `rfkill list` |

## 11. Uninstall

`make uninstall` on either machine removes its services and keeps everything
else. It prints the commands for removing the hotspot, the Pi's hotspot
profile, and the MediaMTX binary too.
