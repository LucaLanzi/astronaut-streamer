"""Tests for config/config.sh: the shell side reads settings exactly like the
Python side, and the templates render completely.

    python3 -m unittest discover -s tests -v
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "common"))
import envconfig  # noqa: E402


def bash(script: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f'. "{ROOT}/config/config.sh"; {script}'],
                          capture_output=True, text=True, env={**os.environ, **(env or {})})


class ShellConfigTests(unittest.TestCase):
    def test_same_values_as_python(self):
        keys = sorted(envconfig.parse_env((ROOT / "config/astronaut.env").read_text()))
        env = {"HOTSPOT_CHANNEL": "11"}
        out = bash("load_config; " + "; ".join(f'printf "%s=%s\\n" {k} "${k}"' for k in keys), env)
        self.assertEqual(out.returncode, 0, out.stderr)
        shell = dict(line.split("=", 1) for line in out.stdout.splitlines())
        old = dict(os.environ)
        try:
            os.environ.update(env)
            python = envconfig.load_config()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(shell, {k: python[k] for k in keys})
        self.assertEqual(shell["HOTSPOT_CHANNEL"], "11", "environment wins")

    def render(self, template: str, extra: str = "") -> subprocess.CompletedProcess:
        return bash(f'load_config; {extra} render_template "{ROOT}/{template}" /dev/stdout')

    def test_mediamtx_config_renders(self):
        out = self.render("jetson/mediamtx.yml.in", 'WEBRTC_IFACES="eth0, wlan0";')
        self.assertEqual(out.returncode, 0, out.stderr)
        yml = out.stdout
        self.assertNotRegex(yml, r"@[A-Z_][A-Z0-9_]*@", "every placeholder filled in")
        self.assertIn("apiAddress: 127.0.0.1:9997", yml)
        self.assertIn("rtspAddress: :8554", yml)
        self.assertIn("webrtcIPsFromInterfacesList: [eth0, wlan0]", yml)
        # MoQ writes into the working folder and hangs on shutdown (MediaMTX 1.21.3).
        self.assertIn("\nmoq: false\n", yml)
        self.assertIn("  astronaut:\n    source: publisher", yml)

    def test_units_render(self):
        for template in ("jetson/astronaut-mediamtx.service.in", "jetson/astronaut-web.service.in",
                         "pi/astronaut-sender.service.in"):
            out = self.render(template, "SERVICE_USER=astro;")
            self.assertEqual(out.returncode, 0, f"{template}: {out.stderr}")
            self.assertNotRegex(out.stdout, r"@[A-Z_][A-Z0-9_]*@", template)
            self.assertIn("ExecStart=", out.stdout)

    def test_missing_value_fails(self):
        out = self.render("jetson/astronaut-web.service.in")   # no SERVICE_USER
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("SERVICE_USER", out.stderr)

    def test_ampersand_stays_literal(self):
        out = bash('X="a&b"; t=$(mktemp); printf "v=@X@\\n" > "$t"; '
                   'render_template "$t" /dev/stdout; rm -f "$t"')
        self.assertEqual(out.stdout.strip(), "v=a&b")


if __name__ == "__main__":
    unittest.main()
