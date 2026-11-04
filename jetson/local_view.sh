#!/usr/bin/env bash
# Watch and listen to the astronaut feed on the Jetson itself, for bench
# testing (PLAN.md §3.5). Needs a monitor and an audio output on the Jetson.
#
#   ./local_view.sh       (or: make view)
#
# Close the window or press q to quit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config

need ffplay
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  die "no display: run this from the Jetson's desktop (with a monitor plugged in)"
fi
URL="rtsp://127.0.0.1:$RTSP_PORT/$STREAM_PATH"
info "Playing $URL (q to quit)"
exec ffplay -hide_banner -loglevel warning -fflags nobuffer -flags low_delay -framedrop \
  -rtsp_transport tcp -window_title "Astronaut feed" "$URL"
