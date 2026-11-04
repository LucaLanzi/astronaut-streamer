"""Tests for config/config.sh: the shell side reads settings exactly like the
Python side, and the templates render completely.

    python3 -m unittest discover -s tests -v
"""

import os
import shutil
import socket
import subprocess
import tempfile
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
        self.assertIn("  talkback:\n", yml)

    def test_units_render(self):
        for template in ("jetson/astronaut-mediamtx.service.in", "jetson/astronaut-web.service.in",
                         "pi/astronaut-sender.service.in", "pi/astronaut-talkback.service.in"):
            out = self.render(template, "SERVICE_USER=astro;")
            self.assertEqual(out.returncode, 0, f"{template}: {out.stderr}")
            self.assertNotRegex(out.stdout, r"@[A-Z_][A-Z0-9_]*@", template)
            self.assertIn("ExecStart=", out.stdout)

    def test_missing_value_fails(self):
        out = self.render("jetson/astronaut-web.service.in")   # no SERVICE_USER
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("SERVICE_USER", out.stderr)

    def test_port_helpers_survive_strict_mode(self):
        """Regression: port_owner on a free port used to end the Jetson
        installer silently (grep finding nothing + pipefail + set -e)."""
        out = bash('set -euo pipefail; owner="$(port_owner tcp 1)"; '
                   'if port_in_use tcp 1; then echo busy; else echo free; fi; echo "done:[$owner]"')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["free", "done:[]"])

    def test_port_in_use_sees_a_listener(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen()
            out = bash(f"set -euo pipefail; port_in_use tcp {s.getsockname()[1]} && echo busy")
        self.assertEqual(out.stdout.strip(), "busy")

    def test_unexpected_failures_are_reported(self):
        out = bash("set -euo pipefail; report_errors; false; echo unreachable")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('"false" failed', out.stderr)
        self.assertNotIn("unreachable", out.stdout)

    def fake_dpkg(self, installed):
        """A PATH dir where dpkg-query knows `installed` and apt-get fails loudly."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        Path(d, "dpkg-query").write_text(
            "#!/bin/sh\nfor p in " + " ".join(installed) + "; do\n"
            '  [ "$3" = "$p" ] && { printf "install ok installed"; exit 0; }\ndone\nexit 1\n')
        Path(d, "apt-get").write_text("#!/bin/sh\necho \"apt-get $*\"\nexit 0\n")
        for f in ("dpkg-query", "apt-get"):
            Path(d, f).chmod(0o755)
        return {"PATH": f"{d}:{os.environ['PATH']}"}

    def test_missing_packages(self):
        out = bash("set -euo pipefail; report_errors; missing_packages ffmpeg iw python3",
                   self.fake_dpkg(["ffmpeg", "python3"]))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["iw"])

    def test_apt_install_skips_apt_when_all_installed(self):
        """Re-running an installer in the field (no internet) must not touch apt."""
        out = bash("set -euo pipefail; report_errors; apt_install ffmpeg iw",
                   self.fake_dpkg(["ffmpeg", "iw"]))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("already installed", out.stdout)
        self.assertNotIn("apt-get", out.stdout)
        out = bash("set -euo pipefail; report_errors; apt_install ffmpeg iw",
                   self.fake_dpkg(["ffmpeg"]))
        self.assertIn("apt-get update", out.stdout)
        self.assertIn("apt-get install -y iw", out.stdout)
        self.assertNotIn("install -y ffmpeg", out.stdout)

    # ------------------------------------------------ make ... ip <role> <addr>
    def set_ips(self, *args):
        local_env = Path(tempfile.mkdtemp()) / "local.env"
        self.addCleanup(shutil.rmtree, local_env.parent)
        out = subprocess.run(["bash", str(ROOT / "tools/set_ips.sh"), *args], capture_output=True,
                             text=True, env={**os.environ, "ASTRO_LOCAL_ENV": str(local_env)})
        saved = local_env.read_text() if local_env.exists() else ""
        return out, envconfig.parse_env(saved)

    def test_set_jetson_addresses(self):
        out, saved = self.set_ips("jetson", "ip", "rover", "192.168.4.97/22", "ip", "hotspot", "192.168.50.1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(saved, {"JETSON_GROUND_IP": "192.168.4.97", "JETSON_GROUND_PREFIX": "22",
                                 "JETSON_HOTSPOT_IP": "192.168.50.1"})
        self.assertIn("192.168.4.97/22", out.stdout)
        self.assertIn("isn't in the hotspot's subnet", out.stderr, "the Pi default no longer matches")

    def test_set_pi_addresses(self):
        out, saved = self.set_ips("pi", "ip", "jetson", "192.168.50.1", "ip", "192.168.50.2")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(saved, {"JETSON_HOTSPOT_IP": "192.168.50.1", "PI_HOTSPOT_IP": "192.168.50.2"})
        self.assertNotIn("subnet", out.stderr)

    def test_no_arguments_keeps_the_rover_defaults(self):
        out, saved = self.set_ips("jetson")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(saved, {})
        self.assertIn("10.110.0.230/24", out.stdout)
        self.assertIn("10.42.0.1/24", out.stdout)

    def test_bad_addresses_are_refused(self):
        for args in (["jetson", "ip", "rover", "192.168.4.999"], ["jetson", "ip", "bogus", "1.2.3.4"],
                     ["pi", "ip"], ["jetson", "rover", "1.2.3.4"], ["pi", "ip", "10.42.0.1"]):
            out, _ = self.set_ips(*args)
            self.assertNotEqual(out.returncode, 0, args)

    # ------------------------------------------- ensure_ground_address (Jetson)
    def fake_network(self, addresses, answering=()):
        """PATH dir with fake ip/ping/nmcli; nmcli logs what it's asked to change."""
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        log = d / "nmcli.log"
        lines = "\n".join(f"2: {dev}    inet {addr} brd 0.0.0.0 scope global" for dev, addr in addresses)
        (d / "ip").write_text(f"#!/bin/sh\nprintf '%s\\n' '{lines}'\n")
        (d / "ping").write_text("#!/bin/sh\nfor a in " + " ".join(answering or ["none"]) +
                                '; do [ "${@: -1}" = "$a" ] && exit 0; done; exit 1\n'.replace("${@: -1}", "$5"))
        (d / "nmcli").write_text(f"""#!/bin/sh
echo "$*" >> {log}
case "$*" in
  "-t -f DEVICE,TYPE,STATE device") printf 'enP8p1s0:ethernet:connected\\nwlP1p1s0:wifi:connected\\n' ;;
  "-g GENERAL.CONNECTION device show enP8p1s0") echo "Wired connection 1" ;;
  "-g ipv4.method connection show Wired connection 1") echo auto ;;
esac
""")
        for f in ("ip", "ping", "nmcli"):
            (d / f).chmod(0o755)
        return {"PATH": f"{d}:{os.environ['PATH']}", "ASTRO_SETTLE_S": "0"}, log

    def ensure(self, env, ip="10.110.0.230", prefix="24"):
        return bash(f"set -euo pipefail; report_errors; ensure_ground_address {ip} {prefix} ''", env)

    def test_ground_address_added_next_to_the_existing_one(self):
        env, log = self.fake_network([("enP8p1s0", "192.168.4.97/22")])
        out = self.ensure(env)
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = log.read_text()
        self.assertIn("connection modify Wired connection 1 +ipv4.addresses 10.110.0.230/24", calls)
        self.assertIn("ipv4.dhcp-timeout infinity", calls)
        self.assertIn("device reapply enP8p1s0", calls)
        self.assertNotIn("-ipv4", calls, "never removes an address")
        self.assertNotIn("connection up", calls, "never re-activates (that would drop SSH)")

    def test_ground_address_already_there(self):
        env, log = self.fake_network([("enP8p1s0", "10.110.0.230/24")])
        out = self.ensure(env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("already on enP8p1s0", out.stdout)
        self.assertFalse(log.exists() and "modify" in log.read_text())

    def test_ground_address_taken_by_another_device(self):
        env, log = self.fake_network([("enP8p1s0", "192.168.4.97/22")], answering=["192.168.4.96"])
        out = self.ensure(env, ip="192.168.4.96", prefix="22")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("another device already answers on 192.168.4.96", out.stderr)
        self.assertFalse(log.exists() and "modify" in log.read_text())

    def test_ampersand_stays_literal(self):
        out = bash('X="a&b"; t=$(mktemp); printf "v=@X@\\n" > "$t"; '
                   'render_template "$t" /dev/stdout; rm -f "$t"')
        self.assertEqual(out.stdout.strip(), "v=a&b")


if __name__ == "__main__":
    unittest.main()
