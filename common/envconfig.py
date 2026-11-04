"""Settings shared by the Pi and the Jetson.

Reads config/astronaut.env, then config/astronaut.local.env on top, then any
environment variable of the same name on top of that. config/config.sh does
the same for the shell scripts, so both sides always agree.

Standard library only, Python 3.10+.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_env(text: str) -> dict[str, str]:
    """Parse KEY=value lines the way `source` would for simple files:
    blank lines and # comments skipped, optional `export`, optional matching
    quotes around the value."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep or not _KEY_RE.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_config(config_dir: Path = CONFIG_DIR) -> dict[str, str]:
    """astronaut.env, overridden by astronaut.local.env (or the file named by
    $ASTRO_LOCAL_ENV), overridden by any environment variable of the same name
    (`VIDEO_WIDTH=640 ./probe_devices.py`)."""
    values: dict[str, str] = {}
    local = os.environ.get("ASTRO_LOCAL_ENV") or str(Path(config_dir) / "astronaut.local.env")
    for path in (Path(config_dir) / "astronaut.env", Path(local)):
        if path.is_file():
            values.update(parse_env(path.read_text()))
    for key in values:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def get_int(cfg: dict[str, str], key: str, default: int) -> int:
    try:
        return int(cfg.get(key) or default)
    except ValueError:
        return default


def get_float(cfg: dict[str, str], key: str, default: float) -> float:
    try:
        return float(cfg.get(key) or default)
    except ValueError:
        return default
