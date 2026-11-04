#!/usr/bin/env bash
# Push this checkout's current commit to the Pi over SSH, then restart the
# sender. Goes through the Jetson (ssh ProxyJump), so it works with the Pi on
# the hotspot and no internet anywhere.
#
#   make push-pi                        # ssh host "astronaut-pi" (docs/SETUP.md §7)
#   PI_SSH=pi@10.42.0.2 make push-pi    # any other ssh destination
#
# The Pi's copy lives in ~/astronaut-streamer and is created if missing. A
# push that would overwrite edits made on the Pi is refused, not forced.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
report_errors

PI_SSH="${PI_SSH:-astronaut-pi}"
PI_REPO="${PI_REPO:-astronaut-streamer}"
[[ "$PI_REPO" =~ ^[A-Za-z0-9._/-]+$ ]] || die "PI_REPO must be a plain path (got '$PI_REPO')"
need git ssh
cd "$ASTRO_ROOT"

commit="$(git rev-parse --short HEAD)"
subject="$(git log -1 --format=%s)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  warn "uncommitted changes here aren't pushed; only commit $commit is"
fi

info "Preparing $PI_SSH:~/$PI_REPO ..."
# A plain repo on branch main that accepts pushes into its checked-out branch
# (updateInstead: the push updates the files, or is refused if they were edited).
# PI_REPO expands here on purpose (it's checked above to be a plain path).
# shellcheck disable=SC2029
branch="$(ssh "$PI_SSH" "set -e
  command -v git >/dev/null || { echo 'git is not installed on the Pi: sudo apt install git' >&2; exit 1; }
  [ -d '$PI_REPO/.git' ] || git init -q -b main '$PI_REPO'
  git -C '$PI_REPO' config receive.denyCurrentBranch updateInstead
  git -C '$PI_REPO' symbolic-ref --short -q HEAD || echo DETACHED")" ||
  die "couldn't reach $PI_SSH over ssh (is ~/.ssh/config set up? docs/SETUP.md §7)"
[ "$branch" = main ] ||
  die "the Pi's copy is on '$branch', not main: on the Pi, run: git -C ~/$PI_REPO checkout main"

info "Pushing $commit ($subject) ..."
if ! git push "$PI_SSH:$PI_REPO" "HEAD:refs/heads/main"; then
  die "push refused. If it says the Pi's working tree has changes, someone edited files on the Pi:
       ssh $PI_SSH 'git -C $PI_REPO status'  (then commit them or: git -C $PI_REPO checkout -- .)
       If it says non-fast-forward, the Pi has commits this checkout doesn't: pull those first."
fi

info "Restarting the sender on the Pi (sudo may ask for the Pi's password) ..."
# shellcheck disable=SC2029
ssh -t "$PI_SSH" "
  echo \"Pi is now at: \$(git -C '$PI_REPO' log -1 --format='%h %s')\"
  if [ -f /etc/systemd/system/astronaut-sender.service ]; then
    sudo systemctl restart astronaut-sender && echo 'astronaut-sender restarted'
  else
    echo 'No sender service on the Pi yet: run make pi there once (needs internet).'
  fi"
echo "Done. If this commit changed the installer or the service file, re-run 'make pi' on the Pi."
