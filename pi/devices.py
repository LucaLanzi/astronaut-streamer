"""Device discovery for the astronaut Pi.

Finds the USB camera, the camera's own microphone, the USB speaker, and the
H.264 encoders ffmpeg can use, and reads Pi health (temperature, throttling,
Wi-Fi link). Shared by probe_devices.py (Phase 0) and sender.py (Phase 3).

Standard library only, Python 3.10+ (Raspberry Pi OS Bookworm ships 3.11).
Parsing is kept in pure functions so it can be unit-tested off the Pi.
"""

from __future__ import annotations

import glob
import os
import re
import resource
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
from envconfig import CONFIG_DIR, load_config, parse_env  # noqa: E402,F401  (re-exported)


# --------------------------------------------------------------------- helpers

def run(cmd: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    """subprocess.run that never raises: a missing program gives returncode
    127 and a timeout gives 124, like the shell."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            cmd, 124, _text(exc.stdout), _text(exc.stderr) + "\n(timed out)")


def _text(data) -> str:
    if data is None:
        return ""
    return data.decode(errors="replace") if isinstance(data, bytes) else data


def _read(path: str | Path, default: str = "") -> str:
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return default


# ------------------------------------------------------------------------ USB

_USB_DEVICE_RE = re.compile(r"\d+-\d+(\.\d+)*")


def usb_device_of(sysfs_path: str) -> str | None:
    """Return the USB device a resolved sysfs path belongs to.

    A camera's video node resolves to something like
    `.../usb1/1-1/1-1.2/1-1.2:1.0` and its microphone to
    `.../usb1/1-1/1-1.2/1-1.2:1.2`; both belong to USB device `.../1-1.2`.
    That is how the camera's own mic is told apart from a USB speaker (or a
    speakerphone with a mic of its own). Returns None for non-USB devices
    such as the Pi's codec, HDMI and headphone devices.
    """
    parts = PurePosixPath(sysfs_path).parts
    deepest = None
    for i, part in enumerate(parts):
        if _USB_DEVICE_RE.fullmatch(part):
            deepest = i
    return str(PurePosixPath(*parts[: deepest + 1])) if deepest is not None else None


def _usb_device_of_link(link: str | Path) -> str | None:
    path = Path(link)
    return usb_device_of(os.path.realpath(path)) if path.exists() else None


# ---------------------------------------------------------------------- video

@dataclass(frozen=True)
class VideoMode:
    fourcc: str
    width: int
    height: int
    fps: float

    def __str__(self) -> str:
        return f"{self.fourcc} {self.width}x{self.height} @ {self.fps:g} fps"


# ffmpeg's v4l2 `-input_format` name for each V4L2 pixel format we use.
FFMPEG_INPUT_FORMAT = {"MJPG": "mjpeg", "YUYV": "yuyv422", "H264": "h264"}

# MJPEG first: the camera compresses it, so it fits through USB at 720p30.
# Raw YUYV is usually limited to ~10 fps at 720p.
PREFERRED_FOURCC = ("MJPG", "YUYV")

_FMT_RE = re.compile(r"^\s*\[\d+\]:\s*'(.{4})'")
_SIZE_RE = re.compile(r"^\s*Size:\s*Discrete\s+(\d+)x(\d+)")
_FPS_RE = re.compile(r"\(([\d.]+)\s*fps\)")


def parse_v4l2_formats(text: str) -> list[VideoMode]:
    """Parse `v4l2-ctl --list-formats-ext` into discrete modes.
    Stepwise/continuous sizes (rare on UVC webcams) are skipped."""
    modes: list[VideoMode] = []
    fourcc = None
    size = None
    for line in text.splitlines():
        m = _FMT_RE.match(line)
        if m:
            fourcc, size = m.group(1).strip(), None
            continue
        m = _SIZE_RE.match(line)
        if m:
            size = (int(m.group(1)), int(m.group(2)))
            continue
        if "Size:" in line:
            size = None
            continue
        if fourcc and size and "Interval:" in line:
            m = _FPS_RE.search(line)
            if m:
                modes.append(VideoMode(fourcc, size[0], size[1], float(m.group(1))))
    return modes


def pick_mode(modes: list[VideoMode], width: int, height: int, fps: float,
              preferred: tuple[str, ...] = PREFERRED_FOURCC) -> VideoMode | None:
    """Closest usable mode to the one asked for.

    In order: preferred pixel format (MJPG over YUYV), then reaching the
    requested frame rate, then the size closest to the requested one, then
    the higher frame rate.
    """
    usable = [m for m in modes if m.fourcc in preferred]
    if not usable:
        return None
    want_area = width * height
    return min(usable, key=lambda m: (
        preferred.index(m.fourcc),
        m.fps + 0.5 < fps,
        abs(m.width * m.height - want_area),
        -m.fps,
    ))


@dataclass
class Camera:
    path: str                   # stable /dev/v4l/by-id path (or /dev/videoN)
    node: str                   # the /dev/videoN it points to right now
    name: str
    usb_device: str | None
    modes: list[VideoMode] = field(default_factory=list)


def list_cameras(by_id_dir: str = "/dev/v4l/by-id",
                 sysfs: str = "/sys/class/video4linux",
                 query_modes: bool = True) -> list[Camera]:
    """USB cameras, by their stable /dev/v4l/by-id path.

    On a Pi, /dev/video10-18 are the Pi's own codec/ISP nodes, and each UVC
    camera creates two nodes (capture + metadata); only `-video-index0`
    links are capture nodes of USB devices.
    """
    cameras: list[Camera] = []
    seen: set[str] = set()  # node names (videoN) already covered by a by-id link
    for link in sorted(glob.glob(os.path.join(by_id_dir, "*-video-index0"))):
        node = os.path.realpath(link)
        seen.add(os.path.basename(node))
        cameras.append(_camera(link, node, sysfs, query_modes))
    # Fallback for a camera without a by-id link: USB video nodes with index 0.
    for dev_dir in sorted(glob.glob(os.path.join(sysfs, "video*"))):
        name = os.path.basename(dev_dir)
        node = "/dev/" + name
        if name in seen or _read(os.path.join(dev_dir, "index"), "0") != "0":
            continue
        if _usb_device_of_link(os.path.join(dev_dir, "device")):
            cameras.append(_camera(node, node, sysfs, query_modes))
    return cameras


def _camera(path: str, node: str, sysfs: str, query_modes: bool) -> Camera:
    dev_dir = os.path.join(sysfs, os.path.basename(node))
    modes: list[VideoMode] = []
    if query_modes:
        out = run(["v4l2-ctl", "-d", node, "--list-formats-ext"])
        modes = parse_v4l2_formats(out.stdout)
    return Camera(
        path=path,
        node=node,
        name=_read(os.path.join(dev_dir, "name"), os.path.basename(path)),
        usb_device=_usb_device_of_link(os.path.join(dev_dir, "device")),
        modes=modes,
    )


def pick_camera(cameras: list[Camera], match: str = "") -> Camera | None:
    """The camera named by `match` (substring of its by-id path or name), or
    else the first camera that has a usable mode."""
    if match:
        needle = match.lower()
        found = [c for c in cameras if needle in (c.path + " " + c.name).lower()]
        return found[0] if found else None
    usable = [c for c in cameras if any(m.fourcc in PREFERRED_FOURCC for m in c.modes)]
    return (usable or cameras or [None])[0]


# ---------------------------------------------------------------------- audio

@dataclass(frozen=True)
class SoundCard:
    index: int
    id: str                     # the bracketed name; ALSA's CARD=<id>
    driver: str
    name: str
    longname: str
    capture_pcms: tuple[int, ...] = ()
    playback_pcms: tuple[int, ...] = ()
    usb_device: str | None = None

    @property
    def capture(self) -> bool:
        return bool(self.capture_pcms)

    @property
    def playback(self) -> bool:
        return bool(self.playback_pcms)

    def alsa_device(self, kind: str) -> str:
        """`plughw:CARD=<id>,DEV=<n>` for 'capture' or 'playback'. Card names
        survive reboots and replugs; card numbers don't. `plughw` lets ALSA
        convert sample rate and channel count."""
        pcms = self.capture_pcms if kind == "capture" else self.playback_pcms
        return f"plughw:CARD={self.id},DEV={pcms[0] if pcms else 0}"

    def matches(self, needle: str) -> bool:
        return needle.lower() in f"{self.id} {self.name} {self.longname}".lower()


_CARD_RE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]:\s*(\S+)\s+-\s+(.*?)\s*$")


def parse_proc_asound_cards(text: str) -> list[dict]:
    """Parse /proc/asound/cards: each card is a header line plus a long-name line."""
    lines = text.splitlines()
    cards = []
    for i, line in enumerate(lines):
        m = _CARD_RE.match(line)
        if m:
            cards.append({
                "index": int(m.group(1)),
                "id": m.group(2),
                "driver": m.group(3),
                "name": m.group(4),
                "longname": lines[i + 1].strip() if i + 1 < len(lines) else "",
            })
    return cards


def _pcm_numbers(card_dir: Path, kind: str) -> tuple[int, ...]:
    numbers = []
    for path in card_dir.glob(f"pcm*{kind}"):
        m = re.fullmatch(rf"pcm(\d+){kind}", path.name)
        if m:
            numbers.append(int(m.group(1)))
    return tuple(sorted(numbers))


def list_sound_cards(proc: str = "/proc/asound",
                     sysfs: str = "/sys/class/sound") -> list[SoundCard]:
    cards = []
    for c in parse_proc_asound_cards(_read(Path(proc, "cards"))):
        card_dir = Path(proc, f"card{c['index']}")
        cards.append(SoundCard(
            **c,
            capture_pcms=_pcm_numbers(card_dir, "c"),
            playback_pcms=_pcm_numbers(card_dir, "p"),
            usb_device=_usb_device_of_link(Path(sysfs, f"card{c['index']}", "device")),
        ))
    return cards


def pick_mic(cards: list[SoundCard], camera_usb: str | None,
             match: str = "") -> tuple[SoundCard | None, str]:
    """The camera's own microphone. Returns (card, how it was chosen).

    With MIC_MATCH set, that card. Otherwise the capture device on the same
    USB device as the camera. If the mic isn't on the camera's USB device
    (some webcams have an internal hub) and there is exactly one USB mic,
    that one. Anything more ambiguous returns None so the user sets MIC_MATCH
    rather than streaming from the speakerphone's mic by mistake.
    """
    capture = [c for c in cards if c.capture]
    if match:
        found = [c for c in capture if c.matches(match)]
        return (found[0], f"MIC_MATCH={match}") if found else (None, f"no mic matches MIC_MATCH={match}")
    if camera_usb:
        same = [c for c in capture if c.usb_device == camera_usb]
        if same:
            return same[0], "same USB device as the camera"
    usb_mics = [c for c in capture if c.usb_device]
    if len(usb_mics) == 1:
        return usb_mics[0], "only USB mic found (not on the camera's USB device)"
    if not usb_mics:
        return None, "no USB mic found"
    return None, f"{len(usb_mics)} USB mics and none on the camera's USB device; set MIC_MATCH"


def pick_speaker(cards: list[SoundCard], camera_usb: str | None,
                 match: str = "") -> tuple[SoundCard | None, str]:
    """The USB speaker: SPEAKER_MATCH if set, else the first USB playback
    device that isn't part of the camera."""
    playback = [c for c in cards if c.playback]
    if match:
        found = [c for c in playback if c.matches(match)]
        return (found[0], f"SPEAKER_MATCH={match}") if found else (None, f"no speaker matches SPEAKER_MATCH={match}")
    usb = [c for c in playback if c.usb_device and c.usb_device != camera_usb]
    if usb:
        return usb[0], "first USB playback device that isn't the camera"
    return None, "no USB speaker found"


def pick_playback(cards: list[SoundCard], match: str) -> SoundCard | None:
    """The playback card whose id or name contains `match` (for talk-back:
    "Headphones" is the Pi's 3.5 mm jack)."""
    found = [c for c in cards if c.playback and c.matches(match)] if match else []
    return found[0] if found else None


@dataclass
class MicResult:
    ok: bool
    max_db: float | None = None
    mean_db: float | None = None
    error: str = ""


def _db(text: str, key: str) -> float | None:
    m = re.search(rf"{key}:\s*(-?inf|-?[\d.]+)\s*dB", text)
    return float(m.group(1)) if m else None


def check_mic(device: str, seconds: float = 3, ffmpeg: str = "ffmpeg") -> MicResult:
    """Record `seconds` through ALSA, the same way the sender will, and
    report the levels. "Device or resource busy" means another program has
    the mic open (an audio server, or a second ffmpeg). Raspberry Pi OS Lite
    runs no audio server, so on the Pi it's usually a leftover ffmpeg."""
    out = run([ffmpeg, "-hide_banner", "-nostats", "-f", "alsa", "-ac", "1",
               "-ar", "48000", "-i", device, "-t", str(seconds),
               "-af", "volumedetect", "-f", "null", "-"], timeout=seconds + 15)
    if "Device or resource busy" in out.stderr:
        return MicResult(False, error="device busy: another program has the mic open")
    max_db, mean_db = _db(out.stderr, "max_volume"), _db(out.stderr, "mean_volume")
    if out.returncode != 0 or max_db is None:
        return MicResult(False, error=_last_line(out.stderr) or f"ffmpeg exit {out.returncode}")
    return MicResult(True, max_db, mean_db)


def beep(device: str, ffmpeg: str = "ffmpeg") -> tuple[bool, str]:
    """Play a one-second 880 Hz tone on an ALSA playback device."""
    out = run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
               "-i", "sine=frequency=880:duration=1", "-ac", "2",
               "-f", "alsa", device], timeout=15)
    return out.returncode == 0, _last_line(out.stderr)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


# -------------------------------------------------------------------- encoders

def parse_encoders(text: str) -> set[str]:
    """Encoder names from `ffmpeg -encoders`."""
    names = set()
    for line in text.splitlines():
        m = re.match(r"^\s*[VAS][.A-Z]{5}\s+(\S+)", line)
        if m and m.group(1) != "=":
            names.add(m.group(1))
    return names


def ffmpeg_encoders(ffmpeg: str = "ffmpeg") -> set[str]:
    return parse_encoders(run([ffmpeg, "-hide_banner", "-encoders"]).stdout)


def encoder_args(encoder: str, bitrate: str = "2M", fps: float = 30,
                 gop: bool = True) -> list[str]:
    """ffmpeg output options for the H.264 encoders the Pi can use.
    Keyframe every second (`-g fps`) so a new viewer starts fast and the
    picture recovers quickly after packet loss."""
    keyframes = ["-g", str(round(fps))] if gop else []
    if encoder == "h264_v4l2m2m":
        # The Pi 4's hardware encoder (/dev/video11, bcm2835-codec).
        return ["-c:v", "h264_v4l2m2m", "-pix_fmt", "yuv420p", "-b:v", bitrate, *keyframes]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "baseline", "-pix_fmt", "yuv420p", "-b:v", bitrate,
            *keyframes, "-bf", "0"]


def parse_progress(text: str) -> dict[str, str]:
    """The last complete block of ffmpeg `-progress` key=value output."""
    last: dict[str, str] = {}
    current: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        current[key] = value.strip()
        if key == "progress":
            last, current = current, {}
    return last or current


@dataclass
class EncodeResult:
    encoder: str
    source: str                 # "synthetic" or the camera mode
    ok: bool
    frames: int = 0
    seconds: float = 0.0        # wall-clock time, including startup
    speed: float | None = None  # multiple of real time, from ffmpeg
    cpu_cores: float = 0.0      # average CPU cores used
    note: str = ""
    error: str = ""

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds else 0.0


def synthetic_input(width: int, height: int, fps: float) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps:g}"]


def camera_input(camera: Camera, mode: VideoMode) -> list[str]:
    return ["-f", "v4l2", "-input_format", FFMPEG_INPUT_FORMAT.get(mode.fourcc, "mjpeg"),
            "-video_size", f"{mode.width}x{mode.height}", "-framerate", f"{mode.fps:g}",
            "-i", camera.path]


def benchmark_encoder(encoder: str, input_args: list[str], source: str,
                      seconds: float = 5, fps: float = 30, bitrate: str = "2M",
                      ffmpeg: str = "ffmpeg") -> EncodeResult:
    """Encode `seconds` of video to nowhere and measure speed and CPU.

    On a synthetic source ffmpeg runs as fast as it can, so `speed` is the
    headroom (2.0 = twice real time). On the live camera `speed` should sit
    at ~1.0; less means the Pi can't keep up at that mode.
    """
    def attempt(gop: bool) -> EncodeResult:
        cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error",
               "-progress", "pipe:1", *input_args, "-t", f"{seconds:g}",
               *encoder_args(encoder, bitrate, fps, gop), "-an", "-f", "null", "-"]
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.monotonic()
        out = run(cmd, timeout=seconds * 6 + 20)
        wall = time.monotonic() - start
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
        progress = parse_progress(out.stdout)
        frames = int(progress.get("frame", "0") or 0)
        if out.returncode != 0 or frames == 0:
            return EncodeResult(encoder, source, False, frames, wall,
                                error=_last_line(out.stderr) or f"ffmpeg exit {out.returncode}")
        speed_text = progress.get("speed", "").rstrip("x")
        try:
            speed = float(speed_text)
        except ValueError:
            speed = None
        return EncodeResult(encoder, source, True, frames, wall, speed,
                            cpu / wall if wall else 0.0)

    result = attempt(gop=True)
    if not result.ok and encoder == "h264_v4l2m2m":
        # Some kernels' bcm2835-codec reject the GOP-size control.
        retry = attempt(gop=False)
        if retry.ok:
            retry.note = "works only without -g (keyframe interval not settable)"
            return retry
    return result


def hw_encoder_node(sysfs: str = "/sys/class/video4linux") -> str | None:
    """The Pi's hardware H.264 encoder node, if the bcm2835-codec driver is loaded."""
    for dev_dir in sorted(glob.glob(os.path.join(sysfs, "video*"))):
        if "encode" in _read(os.path.join(dev_dir, "name")).lower():
            return "/dev/" + os.path.basename(dev_dir)
    return None


# ---------------------------------------------------------------- Pi health

THROTTLE_BITS = {
    0: "under-voltage now",
    1: "CPU frequency capped now",
    2: "throttled now",
    3: "soft temperature limit now",
    16: "under-voltage has occurred",
    17: "CPU frequency capping has occurred",
    18: "throttling has occurred",
    19: "soft temperature limit has occurred",
}


def decode_throttled(value: int) -> list[str]:
    """Meaning of `vcgencmd get_throttled` bits."""
    return [text for bit, text in THROTTLE_BITS.items() if value & (1 << bit)]


def get_throttled() -> int | None:
    out = run(["vcgencmd", "get_throttled"])
    m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out.stdout)
    return int(m.group(1), 16) if m else None


def cpu_temp_c(path: str = "/sys/class/thermal/thermal_zone0/temp") -> float | None:
    raw = _read(path)
    return int(raw) / 1000 if raw.lstrip("-").isdigit() else None


def pi_model() -> str:
    return _read("/proc/device-tree/model").rstrip("\x00") or "unknown (not a Raspberry Pi?)"


def parse_iw_link(text: str) -> dict:
    """Parse `iw dev <iface> link`. Empty dict when not connected."""
    if "Connected to" not in text:
        return {}
    info: dict = {}
    for key, pattern in (("ssid", r"SSID:\s*(.+)"), ("freq_mhz", r"freq:\s*([\d.]+)"),
                         ("signal_dbm", r"signal:\s*(-?\d+)"),
                         ("tx_bitrate", r"tx bitrate:\s*([\d.]+ \S+)")):
        m = re.search(pattern, text)
        if m:
            value = m.group(1).strip()
            info[key] = float(value) if key == "freq_mhz" else int(value) if key == "signal_dbm" else value
    return info


def wifi_link(iface: str = "wlan0") -> dict:
    return parse_iw_link(run(["iw", "dev", iface, "link"]).stdout)


def wifi_iface(configured: str = "", sysfs_net: str = "/sys/class/net") -> str:
    """The configured Wi-Fi interface, else the first wireless one (wlan0 on a Pi)."""
    if configured:
        return configured
    try:
        names = sorted(os.listdir(sysfs_net))
    except OSError:
        names = []
    for name in names:
        if os.path.isdir(os.path.join(sysfs_net, name, "wireless")):
            return name
    return "wlan0"
