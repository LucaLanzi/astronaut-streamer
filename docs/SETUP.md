# astronaut-streamer: setup and operation, top to bottom

Live video and audio from a USB camera on the astronaut's Raspberry Pi, to the
rover's Jetson over the Jetson's Wi-Fi hotspot, then to a browser at the
ground station over the Ubiquiti Prism link.

```
 USB camera + mic          Wi-Fi hotspot "ASTRO-LINK"            Prism point-to-point
 headphones (3.5 mm)          2.4 GHz, WPA2                         link (bridged)
      │                                                                    │
┌─────┴──────────┐  video +  ┌───────────────────────────┐  WebRTC  ┌──────┴──────────┐
│ Raspberry Pi 4B│  mic ───► │ Jetson Orin (JetPack 6)   │ ───────► │ Ground-station  │
│ 10.42.0.2      │ ◄─── talk-│ 10.42.0.1 (hotspot)       │ ◄─────── │ laptop, browser │
│ sender +       │      back │ 10.110.0.230 (Prism side) │ talk-back│ 10.110.0.x      │
│ talk-back      │           │ MediaMTX + dashboard      │          │                 │
└────────────────┘           │ :8080 http  :8443 https   │          └─────────────────┘
                             └───────────────────────────┘
```

**Just want the steps?** [RUN_GUIDE.md](RUN_GUIDE.md) is the short operator
version: setup, testing, and a typical run. This file is the detailed
reference. The design and its reasoning are in [PLAN.md](../PLAN.md). To
prove it all works, follow [TEST_CHECKLIST.md](TEST_CHECKLIST.md).

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
| Headphones | On the Pi's 3.5 mm jack: the astronaut hears the ground station (talk-back). The jack has no mic input; the astronaut talks through the camera mic |
| Portable battery for the Pi | 5 V / 3 A. The dashboard shows under-voltage if it sags |
| Jetson Orin (AGX or Nano) | JetPack 6 (Ubuntu 22.04), working Wi-Fi card, Ethernet to the rover-end Prism |
| Ubiquiti Prism pair | Bridge mode: the ground station and the Jetson's `10.110.0.230` share one subnet |
| Ground-station laptop | Chrome or Firefox, plus a microphone or headset for talk-back. No plugins and no internet needed |
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

**Before you start:** JetPack 6 is installed and the Ethernet is plugged in.
Ethernet is how you reach the Jetson and its internet during the install,
because the last step turns the Wi-Fi into the hotspot.

```bash
make jetson                                # rover addresses (the defaults)
make jetson ip rover 192.168.4.97/22       # or: a bench test on your home network
```

It asks for your password (sudo) and, unless it's in `astronaut.local.env`,
the hotspot password. In about 2 minutes it:

1. saves any `ip` arguments in `config/astronaut.local.env`, and prints the
   addresses in use;
2. installs the packages (ffmpeg, Flask, waitress, cheroot, openssl,
   NetworkManager pieces). This is skipped when they're already there, so
   re-runs work offline;
3. downloads the pinned MediaMTX and checks its SHA-256. It keeps a copy in
   `vendor/`;
4. makes sure the Ethernet has the **ground-side address** `JETSON_GROUND_IP`:
   the Jetson's own address, the one the ground station opens. It only
   **adds** it next to the addresses already there (DHCP or static stay, so
   SSH stays up), and refuses an address another device already answers on;
5. finds the Wi-Fi and Prism-side interfaces (names differ between Jetson
   models);
6. refuses to continue if another program already uses one of its ports, and
   says which;
7. creates the dashboard's HTTPS certificate (self-signed). It covers the
   hotspot, rover and current addresses, so it stays the same on the bench
   and on the rover;
8. writes `/etc/astronaut-streamer/mediamtx.yml` (the feed and the talk-back
   stream) and installs two services, `astronaut-mediamtx` and
   `astronaut-web`, which start at every boot;
9. opens the ports in `ufw` if the firewall is on;
10. checks both services answer, and prints the dashboard addresses:
    `http://…:8080` to watch, `https://…:8443` to watch and talk back;
11. creates the hotspot `ASTRO-LINK` (WPA2, 2.4 GHz ch 6, `10.42.0.1`),
    which also comes up at every boot.

Check it:

```bash
make status    # services, hotspot, connected devices, stream and talk-back state
make ip        # the addresses in use
```

From the ground station, `http://10.110.0.230:8080` shows the dashboard with
**OFFLINE: Waiting for astronaut feed**.

**Bench test at home, then the rover.** Your home network can stand in for
the Prism link: the laptop is the ground station, and the Jetson's Ethernet
uses its home address. Only that address differs. The Pi and the hotspot are
the same in both places.

| | Bench (home network) | Rover (Prism link) |
|---|---|---|
| Install / switch with | `make jetson ip rover 192.168.4.97/22` | `make ip reset && make jetson` |
| Ground-station laptop | anywhere on the home network | a `10.110.0.x` address on the Prism network |
| Dashboard | `http://192.168.4.97:8080`, `https://…:8443` | `http://10.110.0.230:8080`, `https://…:8443` |

The `/22` is your network's mask; leave it off for `/24`. Other addresses:
`make jetson ip hotspot <addr>` moves the hotspot. The Pi then needs
`make pi ip jetson <addr> ip <pi-addr>`, and you should change the Pi first,
while you can still reach it. Without DHCP (the Prism link) the added
address must stay up: check that once on the bench (checklist B.2).

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
effect on the Jetson, and it doesn't need internet once everything is
installed.

## 6. Raspberry Pi

**Flash the card** with Raspberry Pi Imager: *Raspberry Pi OS Lite (64-bit)*.
In the customisation settings:
- hostname `billee-astronaut`
- a username and password
- **your lab/home Wi-Fi** (the Pi needs internet for the install)
- Wi-Fi country
- **enable SSH**

Boot it and SSH in over the lab network.

```bash
sudo apt install -y git make
git clone https://github.com/LucaLanzi/astronaut-streamer.git
cd astronaut-streamer
echo 'HOTSPOT_PSK=the-same-password-as-the-jetson' >> config/astronaut.local.env
make pi                                     # or, if you moved the hotspot:
                                            # make pi ip jetson 192.168.50.1 ip 192.168.50.2
```

`make pi`:

1. sets the Wi-Fi country (Pi OS keeps Wi-Fi off until it's set);
2. installs ffmpeg, v4l-utils, alsa-utils, iw, git and the SSH server, and
   turns SSH on;
3. adds you to the `video` and `audio` groups;
4. saves the hotspot as the Pi's preferred Wi-Fi, with the fixed address
   `10.42.0.2` and power saving off;
5. runs a quick bench check (camera, mic, headphones detection);
6. installs and starts two services that run at every boot:
   - `astronaut-sender`: camera and mic to the Jetson, always on, streaming as
     soon as the Jetson answers;
   - `astronaut-talkback`: plays the ground station's talk-back on the
     headphones whenever someone talks.

Now run the full bench check and keep its summary (the checklist asks for the
numbers):

```bash
make probe       # camera modes, mic level, headphone beep, encoder speed + CPU, Wi-Fi
make headphones  # just the beep in the headphones
```

The talk-back output is the Pi's 3.5 mm jack (`TALKBACK_MATCH=Headphones`).
To use a USB speaker or headset instead, set `TALKBACK_MATCH` to part of its
name (`make probe` lists the names). The volume is `TALKBACK_VOLUME` (default
`90%`).

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
2. Open **`https://10.110.0.230:8443`** in Chrome or Firefox. The first
   time, the browser warns about the certificate (it's the Jetson's own,
   self-signed): *Advanced → Proceed*. Plain `http://10.110.0.230:8080` works
   for watching too, but browsers only allow the microphone on HTTPS.
3. The video starts on its own, muted. Click **🔊 Click for audio** once
   (browsers require a click before playing sound).
4. **Talk-back:** pick a **Microphone** and click **Talk-back: OFF** to turn
   it on. The browser asks for mic permission the first time; then the
   dropdown lists your microphones by name. Wait for *"Live: the astronaut
   hears you"* (the Pi has started playing). **Mute** pauses it instantly, and
   while muted, holding **Space** talks (push-to-talk). Use a headset if you
   can: the browser cancels echo, but a headset is cleaner.

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
| **TALKING** | Your microphone is live to the astronaut |
| Stream panel: Talk-back | *live, the Pi is listening*: they hear you. *the Pi isn't playing it yet*: wait, or restart the Pi's services |
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
- **Addresses:** `make jetson ip …` / `make pi ip …` (§5), `make ip` to see them.
- **Hotspot** (SSID, password, channel): `sudo bash jetson/hotspot.sh up` on
  the Jetson, then `make pi` on the Pi.

**Updating the Pi's code** (it has no internet on the hotspot, and you may be
out of Ethernet ports): from the laptop, in your astronaut-streamer checkout,

```bash
make push-pi
```

It pushes your current commit to the Pi's copy (`~/astronaut-streamer`, on
branch `main`) over SSH through the Jetson, the same `astronaut-pi` jump as
above, then restarts the sender. No internet is needed anywhere, and it works
for commits you haven't pushed to GitHub. It refuses rather than overwrite
files someone edited on the Pi. `PI_SSH=user@host make push-pi` targets
another ssh destination. Only committed changes go: commit first. If the
commit changed an installer or the service file, run `make pi` on the Pi
afterwards. That needs internet only if new packages are needed.

**Testing without the camera:** `make test-pattern` on the Pi sends colour bars
and a 440 Hz tone. It pauses the service and starts it again when you press
Ctrl-C.

**Watching on the Jetson itself** (bench, with a monitor and headset):
`make view`.

## 9. Command reference

| Command | Where | What it does |
|---|---|---|
| `make jetson` | Jetson | Install or update everything, hotspot included |
| `make jetson ip rover <addr>` / `ip hotspot <addr>` | Jetson | The same, with other addresses (saved) |
| `make hotspot` | Jetson | Hotspot state and connected devices |
| `sudo bash jetson/hotspot.sh up\|start\|stop\|remove` | Jetson | Change the hotspot |
| `make view` | Jetson | Watch and listen locally (ffplay) |
| `make pi` | Pi | Install or update everything: sender and talk-back services |
| `make pi ip <pi-addr>` / `ip jetson <addr>` | Pi | The same, with other hotspot addresses (saved) |
| `make headphones` | Pi | Play a test tone on the talk-back output |
| `make probe` | Pi | Full bench check (pauses the sender) |
| `make test-pattern` | Pi | Colour bars + tone instead of the camera (pauses the sender) |
| `make push-pi` | laptop | Push the current commit to the Pi through the Jetson and restart the sender |
| `make ip` / `make ip reset` | either | Show the addresses in use / back to the rover defaults |
| `make status` | either | Services, Wi-Fi/hotspot, stream and talk-back state |
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
| No talk-back panel, just a link to a secure page | You're on `http://` | Open the `https://…:8443` address and accept the certificate once |
| "Couldn't start talk-back" / mic blocked | Browser's site settings (lock icon) | Allow the microphone for the site, then turn talk-back on again |
| Talk-back stays at "waiting for the Pi" | On the Pi: `make status` (talk-back log lines) | `make restart` on the Pi; `make probe` checks the headphones output |
| Talk-back live, but nothing in the headphones | On the Pi: `make headphones` | Plug fully in; raise `TALKBACK_VOLUME`; `alsamixer -c Headphones` |
| Talk-back choppy or crackling | Pi panel: Wi-Fi signal | Move closer; raise `TALKBACK_BUFFER_MS` (default 80) on the Pi, then `make restart` |
| `make jetson` says another device answers on the ground-side address | The address is taken (e.g. by the Pi) | Use the Jetson's own address: `make jetson ip rover <addr>` |

## 11. Uninstall

`make uninstall` on either machine removes its services and keeps everything
else. It prints the commands for removing the hotspot, the Pi's hotspot
profile, and the MediaMTX binary too.
