#!/usr/bin/env bash
# What astronaut-streamer has installed and running on this machine.
#
#   ./tools/status.sh     (or: make status)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config

installed=()
for unit in astronaut-mediamtx astronaut-web astronaut-sender; do
  [ -f "/etc/systemd/system/$unit.service" ] && installed+=("$unit")
done
if [ "${#installed[@]}" -eq 0 ]; then
  echo "No astronaut-streamer services on this machine."
  echo "Install: 'make jetson' on the Jetson, 'make pi' on the Pi (docs/SETUP.md)."
  exit 0
fi

echo "Services:"
for unit in "${installed[@]}"; do
  printf '  %-20s %s\n' "$unit" "$(systemctl is-active "$unit")"
done

if [ -f /etc/systemd/system/astronaut-web.service ]; then   # the Jetson
  echo
  bash "$ASTRO_ROOT/jetson/hotspot.sh" status
  echo
  echo "Stream (from the dashboard):"
  if ! curl -fsS --max-time 3 "http://127.0.0.1:$WEB_PORT/api/status" | python3 -c '
import json, sys
s = json.load(sys.stdin)
f, p = s.get("feed") or {}, s.get("pi")
print("  feed:     %s (%s)" % ("LIVE" if f.get("live") else "offline", f.get("message")))
if f.get("live"):
    print("  tracks:   %s   bitrate: %s kbit/s   viewers: %s" % (
        " + ".join(f.get("tracks") or []), f.get("bitrate_kbps"), f.get("viewers")))
if p:
    wifi = p.get("wifi") or {}
    print("  pi:       %s  %s  signal %s dBm  %s C  last heartbeat %s s ago" % (
        p.get("state"), p.get("ip"), wifi.get("signal_dbm", "?"), p.get("temp_c"), p.get("age_s")))
    if p.get("last_error"):
        print("  pi error: %s" % p["last_error"])
else:
    print("  pi:       no heartbeat yet")
'; then
    echo "  the dashboard isn't answering on port $WEB_PORT"
  fi
  echo
  echo "Ground station: http://$JETSON_GROUND_IP:$WEB_PORT"
fi

if [ -f /etc/systemd/system/astronaut-sender.service ]; then   # the Pi
  echo
  iface="$(wifi_iface "$PI_WIFI_IFACE")"
  echo "Wi-Fi ($iface):"
  iw dev "$iface" link 2>/dev/null | sed -n 's/^\s*\(Connected to\|SSID\|signal\|tx bitrate\)/  \1/p'
  echo
  echo "Last sender log lines:"
  journalctl -u astronaut-sender -n 15 --no-pager -o cat 2>/dev/null | sed 's/^/  /'
fi
