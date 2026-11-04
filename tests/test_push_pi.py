"""Tests for tools/push_pi.sh (`make push-pi`), without a Pi.

A fake `ssh` runs each remote command on this machine inside a temporary
"Pi home" folder, so git and the script behave exactly as over the real link.

    python3 -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_SSH = """#!/usr/bin/env bash
# ssh stand-in: skip options and the host, run the command in $FAKE_PI_HOME.
while [ $# -gt 0 ]; do
  case "$1" in
    -o|-p|-i|-F|-J|-l) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
shift   # the host
cd "$FAKE_PI_HOME" && exec bash -c "$*"
"""


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs git and a git checkout")
class PushPiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(FAKE_SSH)
        (bin_dir / "ssh").chmod(0o755)
        self.pi_home = self.tmp / "pi-home"
        self.pi_home.mkdir()
        self.env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "FAKE_PI_HOME": str(self.pi_home), "PI_SSH": "astronaut-pi",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@t"}
        self.head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                                   capture_output=True, text=True, check=True).stdout.strip()

    def push(self):
        return subprocess.run(["bash", str(ROOT / "tools/push_pi.sh")], capture_output=True,
                              text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=120)

    def pi_git(self, *args):
        return subprocess.run(["git", "-C", str(self.pi_home / "astronaut-streamer"), *args],
                              capture_output=True, text=True, env=self.env)

    def test_first_push_creates_the_copy_and_checks_out_files(self):
        out = self.push()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.pi_git("rev-parse", "HEAD").stdout.strip(), self.head)
        tracked = self.pi_git("ls-files").stdout.split()
        self.assertIn("pi/sender.py", tracked)
        self.assertTrue((self.pi_home / "astronaut-streamer/pi/sender.py").exists(),
                        "the push updated the Pi's files, not just its history")
        self.assertEqual(self.pi_git("status", "--porcelain").stdout, "")
        self.assertIn("No sender service on the Pi yet", out.stdout)

    def test_second_push_is_harmless(self):
        self.assertEqual(self.push().returncode, 0)
        out = self.push()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.pi_git("rev-parse", "HEAD").stdout.strip(), self.head)

    def test_refuses_to_overwrite_edits_made_on_the_pi(self):
        self.assertEqual(self.push().returncode, 0)
        # Move the Pi's copy back one commit and edit a file there, so a new
        # push would have to change it.
        self.pi_git("reset", "-q", "--hard", "HEAD~1")
        edited = self.pi_home / "astronaut-streamer/README.md"
        edited.write_text(edited.read_text() + "\nedited on the Pi\n")
        out = self.push()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("push refused", out.stderr)
        self.assertIn("edited on the Pi", edited.read_text(), "the Pi's edit is kept")

    def test_wrong_branch_is_reported(self):
        self.assertEqual(self.push().returncode, 0)
        self.pi_git("checkout", "-q", "-b", "experiment")
        out = self.push()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("on 'experiment', not main", out.stderr)


if __name__ == "__main__":
    unittest.main()
