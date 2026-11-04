#!/usr/bin/env bash
# Prepare `make e2e` on a dev laptop: a private Python environment (Flask,
# waitress, Playwright) and the pinned MediaMTX in vendor/. No sudo; nothing
# is installed system-wide.
#
#   E2E_VENV=.e2e-venv bash tools/e2e_setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config
report_errors

VENV="${E2E_VENV:-$ASTRO_ROOT/.e2e-venv}"
need python3 curl tar
if [ ! -x "$VENV/bin/python" ]; then
  info "Creating the test environment in $VENV..."
  python3 -m venv "$VENV" || die "python3 -m venv failed (sudo apt install python3-venv)"
fi
if ! "$VENV/bin/python" -c "import flask, waitress, playwright" 2>/dev/null; then
  info "Installing Flask, waitress and Playwright into it..."
  "$VENV/bin/pip" install -q --disable-pip-version-check flask waitress playwright
fi
if ! command -v ffmpeg >/dev/null 2>&1 && ! "$VENV/bin/python" -c "import imageio_ffmpeg" 2>/dev/null; then
  info "No ffmpeg on PATH: installing a static build (imageio-ffmpeg) into the test environment..."
  "$VENV/bin/pip" install -q --disable-pip-version-check imageio-ffmpeg
fi

tarball="$(fetch_mediamtx)"
dir="$ASTRO_ROOT/vendor/mediamtx-${MEDIAMTX_VERSION}-$(mediamtx_arch)"
if [ ! -x "$dir/mediamtx" ]; then
  mkdir -p "$dir"
  tar -xzf "$tarball" -C "$dir" mediamtx
fi
info "Ready: $dir/mediamtx"
