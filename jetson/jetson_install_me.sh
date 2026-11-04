#!/usr/bin/env bash
# One-time setup for the Jetson: JetPack 6 / Ubuntu 22.04 (PLAN.md §3.3-3.5).
#
#   sudo ./jetson_install_me.sh    packages, MediaMTX, dashboard, services, firewall
#
# The hotspot is separate: sudo ./hotspot.sh up  (`make jetson` runs both).
# Safe to re-run: it rewrites the config and restarts the services, which is
# also how changes to config/astronaut.env take effect.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config
report_errors

UNITS=(astronaut-mediamtx astronaut-web)

case "${1:-}" in
  -h | --help) sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
  "") ;;
  *) die "unknown option: $1 (see --help)" ;;
esac

require_root
SERVICE_USER="${SUDO_USER:-}"
{ [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != root ]; } ||
  die "run this with sudo from your normal account: the dashboard runs as that user"

mediamtx_arch >/dev/null || die "unsupported CPU architecture: $(uname -m)"

if [ -f /etc/nv_tegra_release ]; then
  info "Jetson: $(head -1 /etc/nv_tegra_release | cut -c1-60)"
else
  warn "this doesn't look like a Jetson (no /etc/nv_tegra_release); continuing anyway"
fi

# ----------------------------------------------------------------- packages
info "Installing packages (needs internet)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ffmpeg python3 python3-flask python3-waitress curl ca-certificates \
  iw iproute2 network-manager dnsmasq-base

# ----------------------------------------------------------------- MediaMTX
# The pinned release, downloaded once into vendor/ and checked against the
# SHA-256 in config/config.sh.
if /usr/local/bin/mediamtx --help 2>&1 | grep -q "MediaMTX ${MEDIAMTX_VERSION},"; then
  info "MediaMTX $MEDIAMTX_VERSION is already installed"
else
  tarball="$(fetch_mediamtx)"
  chown -R "$SERVICE_USER": "$ASTRO_ROOT/vendor"
  tmp="$(mktemp -d)"
  tar -xzf "$tarball" -C "$tmp" mediamtx
  install -m 0755 "$tmp/mediamtx" /usr/local/bin/mediamtx
  rm -rf "$tmp"
  info "Installed $(/usr/local/bin/mediamtx --help 2>&1 | grep -o 'MediaMTX v[^,]*')"
fi

# --------------------------------------------------------------- interfaces
HOTSPOT_IFACE="$(wifi_iface "$JETSON_HOTSPOT_IFACE")"
GROUND_IFACE="${JETSON_GROUND_IFACE:-$(iface_with_ip "$JETSON_GROUND_IP")}"
[ -n "$HOTSPOT_IFACE" ] || warn "no Wi-Fi interface found: the hotspot can't run (PLAN.md §8: USB Wi-Fi fallback)"
if [ -z "$GROUND_IFACE" ]; then
  warn "no interface has JETSON_GROUND_IP=$JETSON_GROUND_IP. That setting is this Jetson's OWN"
  warn "address on the Prism side (normally 10.110.0.230), not the Pi's. Check config/astronaut*.env,"
  warn "and give the Jetson's Ethernet that address (docs/SETUP.md §5), then re-run this installer."
  warn "Continuing: the dashboard still works on every address the Jetson has."
fi
# Offer WebRTC only on the Prism-side and hotspot interfaces when both are
# known; otherwise every interface, so the ground station is never left out.
# (WEBRTC_IFACES is read by render_template.)
# shellcheck disable=SC2034
if [ -n "$HOTSPOT_IFACE" ] && [ -n "$GROUND_IFACE" ]; then
  WEBRTC_IFACES="$GROUND_IFACE, $HOTSPOT_IFACE"
else
  WEBRTC_IFACES=""
fi
info "Interfaces: ground=${GROUND_IFACE:-?}  hotspot=${HOTSPOT_IFACE:-?}"

# ------------------------------------------------------------------ ports
info "Stopping old astronaut-streamer services (if any) to check ports..."
systemctl stop "${UNITS[@]}" 2>/dev/null || true

busy=()
for spec in "tcp $WEB_PORT WEB_PORT" "tcp $RTSP_PORT RTSP_PORT" "tcp $WEBRTC_PORT WEBRTC_PORT" \
  "tcp $HLS_PORT HLS_PORT" "tcp $MEDIAMTX_API_PORT MEDIAMTX_API_PORT" \
  "udp $WEBRTC_UDP_PORT WEBRTC_UDP_PORT" "udp $SRT_PORT SRT_PORT"; do
  read -r proto port name <<<"$spec"
  if port_in_use "$proto" "$port"; then
    owner="$(port_owner "$proto" "$port")"
    busy+=("$port/$proto ($name) is used by ${owner:-another program}")
  fi
done
if [ "${#busy[@]}" -gt 0 ]; then
  printf '  %s\n' "${busy[@]}" >&2
  die "free those ports, or pick others in config/astronaut.local.env, then re-run"
fi

# ------------------------------------------------------- config + services
info "Writing /etc/astronaut-streamer/mediamtx.yml and the systemd services..."
mkdir -p /etc/astronaut-streamer
render_template "$SCRIPT_DIR/mediamtx.yml.in" /etc/astronaut-streamer/mediamtx.yml
chmod 0644 /etc/astronaut-streamer/mediamtx.yml
render_template "$SCRIPT_DIR/astronaut-mediamtx.service.in" /etc/systemd/system/astronaut-mediamtx.service
render_template "$SCRIPT_DIR/astronaut-web.service.in" /etc/systemd/system/astronaut-web.service
systemctl daemon-reload
systemctl enable "${UNITS[@]}" >/dev/null 2>&1
systemctl restart "${UNITS[@]}"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
  info "ufw is active: opening the stream and dashboard ports..."
  for p in "$WEB_PORT/tcp" "$WEBRTC_PORT/tcp" "$WEBRTC_UDP_PORT/udp" "$HLS_PORT/tcp" \
    "$RTSP_PORT/tcp" "$SRT_PORT/udp"; do
    ufw allow "$p" comment astronaut-streamer >/dev/null
  done
fi

# ------------------------------------------------------------------ verify
info "Checking the services answer..."
ok=true
wait_for() {  # $1 = URL; up to 10 s
  local _
  for _ in $(seq 1 20); do
    curl -fsS -o /dev/null --max-time 1 "$1" 2>/dev/null && return 0
    sleep 0.5
  done
  return 1
}
if wait_for "http://127.0.0.1:$MEDIAMTX_API_PORT/v3/paths/list"; then
  info "MediaMTX: running"
else
  ok=false
  warn "MediaMTX isn't answering. Last log lines:"
  journalctl -u astronaut-mediamtx -n 20 --no-pager >&2 || true
fi
if wait_for "http://127.0.0.1:$WEB_PORT/healthz"; then
  info "Dashboard: running"
else
  ok=false
  warn "The dashboard isn't answering. Last log lines:"
  journalctl -u astronaut-web -n 20 --no-pager >&2 || true
fi
$ok || die "setup finished but a service isn't running; see the log lines above"

echo
echo "Done. The services start by themselves at every boot."
echo "Dashboard addresses:"
ip -o -4 addr show | awk -v port="$WEB_PORT" -v g="$JETSON_GROUND_IP" '$2 != "lo" {
  split($4, a, "/"); note = (a[1] == g) ? "   <- ground station (Prism link)" : ""
  printf "  http://%s:%s   (%s)%s\n", a[1], port, $2, note }'
echo "Next: the hotspot (sudo ./hotspot.sh up, or 'make jetson' does it), then the Pi."
