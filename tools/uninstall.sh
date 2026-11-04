#!/usr/bin/env bash
# Remove astronaut-streamer's services from this machine.
#
#   sudo ./tools/uninstall.sh     (or: make uninstall)
#
# Keeps the apt packages, /usr/local/bin/mediamtx, your settings, and the Wi-Fi
# setup (see the message at the end for removing those too).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
require_root

for unit in astronaut-talkback astronaut-sender astronaut-web astronaut-mediamtx; do
  if [ -f "/etc/systemd/system/$unit.service" ]; then
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$unit.service"
    info "Removed $unit"
  fi
done
systemctl daemon-reload
rm -rf /etc/astronaut-streamer

cat <<'EOF'
Done. Still on this machine, if you want them gone too:
  Jetson hotspot:        sudo jetson/hotspot.sh remove
  Pi hotspot profile:    sudo nmcli connection delete astro-link
  MediaMTX binary:       sudo rm /usr/local/bin/mediamtx
EOF
