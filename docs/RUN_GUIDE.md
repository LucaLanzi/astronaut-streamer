# Run guide

How to set up, test, and run the astronaut stream. Every command says where
it runs:

- **[Jetson]**: in `~/billee-hardware-2027` on the Jetson
- **[Pi]**: in `~/astronaut-streamer` on the Pi
- **[Laptop]**: on the ground-station laptop

On the Jetson every command is `make astronaut <command>` (from the
billee-hardware-2027 root). On the Pi it's `make <command>` (inside
astronaut-streamer). Same commands either way.

**Addresses.** The defaults are the rover's. Change them with `ip` arguments
(Part 2.2). `make ip` shows the ones in use.

| | Rover default |
|---|---|
| Jetson, Prism side (what the ground station opens) | `10.110.0.230` |
| Jetson hotspot `ASTRO-LINK` | `10.42.0.1` |
| Pi on the hotspot | `10.42.0.2` |
| Dashboard (watch) | `http://10.110.0.230:8080` |
| Dashboard (watch + talk-back) | `https://10.110.0.230:8443` |

---

## Part 1: One-time setup

### 1.1 Jetson

Needs internet once (through Ethernet: the Wi-Fi becomes the hotspot).

1. **Get the code**: **[Jetson]**
   ```bash
   cd ~/billee-hardware-2027 && make rover
   ```
   Updates the repo and all submodules.

2. **Set the hotspot password** (8+ characters; the Pi needs the same one): **[Jetson]**
   ```bash
   echo 'HOTSPOT_PSK=your-password' >> astronaut-streamer/config/astronaut.local.env
   ```

3. **Install**: **[Jetson]**
   ```bash
   make astronaut jetson
   ```
   Installs everything and creates the hotspot.
   ✅ Ends with `Done.` and the dashboard addresses.

4. **Check**: **[Jetson]**
   ```bash
   make astronaut status
   ```
   ✅ Services `active`, `Hotspot: UP`.

### 1.2 Pi

Needs internet once, so do this while the Pi is still on your home Wi-Fi.

1. **Flash the card** with Raspberry Pi Imager: *Raspberry Pi OS Lite (64-bit)*.
   In its settings:
   - hostname `billee-astronaut`
   - a user and password
   - your home Wi-Fi and the Wi-Fi country
   - **SSH on**

2. **Log in and get the code**: **[Laptop]**, then on the Pi
   ```bash
   ssh <pi-user>@billee-astronaut.local
   sudo apt install -y git make
   git clone https://github.com/LucaLanzi/astronaut-streamer.git && cd astronaut-streamer
   ```

3. **Same hotspot password as the Jetson**: **[Pi]**
   ```bash
   echo 'HOTSPOT_PSK=your-password' >> config/astronaut.local.env
   ```

4. **Install**: **[Pi]**
   ```bash
   make pi
   ```
   Installs the camera sender and the talk-back player, both starting at boot.
   ✅ Ends with `Done.`

5. **Bench check**: plug in the camera and headphones (3.5 mm jack). **[Pi]**
   ```bash
   make probe
   ```
   ✅ `0 problem(s)`, and you hear a beep in the headphones.

6. **Move it to the hotspot**: take it near the Jetson. **[Pi]**
   ```bash
   sudo reboot
   ```
   It joins `ASTRO-LINK` and starts streaming by itself. From now on, reach
   it through the Jetson (1.3).

### 1.3 Ground-station laptop

1. **SSH shortcuts.** Add to `~/.ssh/config` (use the Jetson's address: `10.110.0.230` on the rover):
   ```
   Host jetson
       HostName 10.110.0.230
       User <jetson-user>

   Host astronaut-pi
       HostName 10.42.0.2
       User <pi-user>
       ProxyJump jetson
   ```
   Then once: `ssh-copy-id jetson && ssh-copy-id astronaut-pi`

2. **Accept the dashboard's certificate once.** Open `https://<jetson>:8443`.
   The browser warns because the certificate is the Jetson's own:
   *Advanced → Proceed*. That's needed for talk-back (browsers only allow
   the microphone on HTTPS).

---

## Part 2: Testing

### 2.1 On a laptop, no hardware

**[Laptop]**, in a copy of astronaut-streamer:
```bash
make test     # unit tests: OK
make e2e      # whole pipeline + talk-back in a headless browser: "N/N checks passed"
```

### 2.2 Bench test at home

Your home network stands in for the Prism link: the Jetson's Ethernet uses
its home address, and the laptop on the same network is the ground station.

1. **Point the Jetson at your home network**: **[Jetson]**
   ```bash
   make astronaut jetson ip rover 192.168.4.97/22
   ```
   `ip rover` is the Jetson's own Ethernet address (with your network's mask).
   It's saved, and added next to the current address, so SSH stays up.

2. **Watch**: **[Laptop]** open `http://192.168.4.97:8080`.
   ✅ **LIVE** with the camera. Click **🔊 Click for audio** to hear the Pi's mic.

3. **Talk back**: **[Laptop]** open `https://192.168.4.97:8443`.
   1. Pick your **Microphone**.
   2. Click **Talk-back: OFF** to turn it on (the browser asks for permission the first time).
   3. Wait for **"Live: the astronaut hears you."**

   ✅ You hear yourself in the Pi's headphones. **Mute** pauses it. While
   muted, hold **Space** to talk.

4. **Test without the camera** (optional): **[Pi]**
   ```bash
   make test-pattern     # colour bars + tone; Ctrl-C to go back to the camera
   ```

5. **Send a code change to the Pi** (no internet needed): **[Laptop]**, in your astronaut-streamer copy
   ```bash
   git commit -am "what changed" && make push-pi
   ```

**Other addresses**, if you need them:
```bash
make astronaut jetson ip hotspot 192.168.50.1     # [Jetson] move the hotspot
make pi ip jetson 192.168.50.1 ip 192.168.50.2     # [Pi] follow it (do the Pi first)
make astronaut ip                                  # [Jetson] show what's in use
```

**Back to the rover addresses** before a field day: **[Jetson]**
```bash
make astronaut ip reset && make astronaut jetson
```

---

## Part 3: A typical run on the ground

**Before you leave**
1. **[Jetson]** `make astronaut ip`: shows `10.110.0.230`. If not, run
   `make astronaut ip reset && make astronaut jetson`.
2. Charge the Pi's battery. Pack the headphones.

**On site**
1. **Power on** the rover (Jetson), both Prism radios, and the Pi, in any
   order. Everything starts by itself in about a minute.
2. **[Laptop]** Plug into the ground Prism radio, with an address in
   `10.110.0.x`.
   ✅ `ping 10.110.0.230` answers.
3. **[Laptop]** Open **`https://10.110.0.230:8443`**.
   ✅ **LIVE**. Click **🔊 Click for audio**.
4. **To talk:** turn **Talk-back** on and wait for *"the astronaut hears
   you"*. Use **Mute** or hold **Space** between messages.
5. **Keep an eye on** the *Astronaut Pi* panel: Wi-Fi signal (-80 dBm or
   worse = weak), CPU temperature, Throttling.

**If something's wrong**

| You see | Do this |
|---|---|
| **OFFLINE**, "Waiting for astronaut feed" | Pi off, out of range, or rebooting: move closer, wait 1 min |
| **PI SILENT** | Same as above; check the Pi's battery |
| **SERVER DOWN** | **[Laptop]** `ssh -t jetson 'make -C billee-hardware-2027/astronaut-streamer restart'` |
| **DASHBOARD DOWN** | Prism link down: check the radios and the laptop's cable/address |
| **NO AUDIO** | The camera mic failed; video keeps going. Replug the camera |
| Black video but **LIVE** | Click **Compatibility (HLS)** (2-4 s delay, works anywhere) |
| Talk-back says "waiting for the Pi" | **[Laptop]** `ssh -t astronaut-pi 'make -C astronaut-streamer restart'` |
| No sound in the headphones | Volume: set `TALKBACK_VOLUME` in the Pi's `astronaut.local.env`, then restart; check the plug |

More detail: `make status` (either machine), `make logs` (either machine),
and [SETUP.md §10](SETUP.md#10-troubleshooting).

**After**
1. Turn **Talk-back** off.
2. **[Laptop]** `ssh -t astronaut-pi sudo poweroff`, then power off the rover.
