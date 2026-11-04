#!/usr/bin/env bash
# One-time setup for the astronaut Pi: Raspberry Pi 4B, Raspberry Pi OS Lite
# 64-bit (Bookworm) (PLAN.md §3.2).
#
#   sudo ./pi_install_me.sh               packages, Wi-Fi country, SSH, hotspot
#                                         profile, bench check, sender service
#   sudo ./pi_install_me.sh --connect     ...and switch to the hotspot now
#   sudo ./pi_install_me.sh --no-service  ...without installing the sender service
#
# Run it while the Pi still has internet (apt needs it). Afterwards the Pi joins
# the hotspot by itself whenever it's in range, at the fixed address
# PI_HOTSPOT_IP, and the sender service streams as soon as the Jetson answers.
# Safe to re-run. Settings: config/astronaut.env (+ astronaut.local.env).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config
report_errors

CON_NAME="astro-link"
UNIT="astronaut-sender"
CONNECT=false
SERVICE=true

usage() {
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --connect) CONNECT=true ;;
    --no-service) SERVICE=false ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

require_root
SERVICE_USER="${SUDO_USER:-}"
{ [ -n "$SERVICE_USER" ] && [ "$SERVICE_USER" != root ]; } ||
  die "run this with sudo from your normal account: the sender runs as that user"

# Raspberry Pi OS keeps Wi-Fi switched off (rfkill) until a Wi-Fi country is set.
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || warn "couldn't set the Wi-Fi country to $WIFI_COUNTRY"
fi
if command -v rfkill >/dev/null 2>&1; then
  rfkill unblock wifi || true
fi
need nmcli
IFACE="$(wifi_iface "$PI_WIFI_IFACE")"
[ -n "$IFACE" ] || die "no Wi-Fi interface found (see 'nmcli device')"
nm_has_wifi "$IFACE" || die "no NetworkManager Wi-Fi interface '$IFACE' (see 'nmcli device'; set PI_WIFI_IFACE)"
ask_psk

apt_install ffmpeg v4l-utils alsa-utils iw openssh-server python3 git
# vcgencmd (throttle/under-voltage flags) is normally preinstalled; the package
# was renamed from libraspberrypi-bin to raspi-utils.
if ! command -v vcgencmd >/dev/null 2>&1; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y raspi-utils ||
    DEBIAN_FRONTEND=noninteractive apt-get install -y libraspberrypi-bin ||
    warn "vcgencmd isn't available: the Pi's throttle/under-voltage flags won't be reported"
fi

# Pi OS Lite ships SSH turned off unless it was enabled in Raspberry Pi Imager.
# The ground station reaches the Pi through the Jetson (ssh -J), so it needs it.
info "Enabling SSH..."
systemctl enable --now ssh

info "Adding $SERVICE_USER to the video and audio groups (camera and mic access)..."
usermod -aG video,audio "$SERVICE_USER"

info "Saving the hotspot '$HOTSPOT_SSID' as $CON_NAME on $IFACE ($PI_HOTSPOT_IP)..."
if nm_has_connection "$CON_NAME"; then
  nmcli connection delete "$CON_NAME" >/dev/null
fi
# Highest autoconnect priority, so the Pi prefers the hotspot over any lab/home
# network it knows. Fixed address below NetworkManager's DHCP range (.10-.254).
# never-default: the hotspot only carries the stream, so it never takes over
# the Pi's internet route while another network is still connected.
nmcli connection add type wifi ifname "$IFACE" con-name "$CON_NAME" \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  ssid "$HOTSPOT_SSID" \
  802-11-wireless.powersave 2 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PSK" \
  ipv4.method manual ipv4.addresses "$PI_HOTSPOT_IP/$HOTSPOT_PREFIX_LEN" \
  ipv4.never-default yes \
  ipv6.method disabled >/dev/null

# The bench check needs the camera and mic, so the sender must not hold them.
systemctl stop "$UNIT" 2>/dev/null || true
info "Bench check (encoder benchmark skipped; 'make probe' runs the full check):"
sudo -u "$SERVICE_USER" python3 "$SCRIPT_DIR/probe_devices.py" --no-encode || true

if $SERVICE; then
  info "Installing the $UNIT service..."
  render_template "$SCRIPT_DIR/astronaut-sender.service.in" "/etc/systemd/system/$UNIT.service"
  systemctl daemon-reload
  systemctl enable "$UNIT" >/dev/null 2>&1
  systemctl restart "$UNIT"
  sleep 1
  systemctl is-active --quiet "$UNIT" ||
    { journalctl -u "$UNIT" -n 20 --no-pager >&2 || true; die "the $UNIT service didn't start; see the log above"; }
  info "$UNIT is running; it streams as soon as the Jetson answers."
fi

if $CONNECT; then
  echo
  warn "switching Wi-Fi to '$HOTSPOT_SSID': if you're connected to this Pi over Wi-Fi, this session will drop"
  nmcli connection up "$CON_NAME" >/dev/null ||
    die "couldn't join '$HOTSPOT_SSID'; is the Jetson's hotspot up and in range? (on the Jetson: make hotspot)"
  if ping -c 2 -W 2 "$JETSON_HOTSPOT_IP" >/dev/null 2>&1; then
    info "Joined '$HOTSPOT_SSID'; the Jetson ($JETSON_HOTSPOT_IP) answers."
  else
    warn "joined '$HOTSPOT_SSID', but the Jetson ($JETSON_HOTSPOT_IP) doesn't answer ping"
  fi
fi

cat <<EOF

Done.
  - The Pi joins '$HOTSPOT_SSID' as $PI_HOTSPOT_IP whenever it's in range.
    To switch now: sudo nmcli connection up $CON_NAME
  - SSH is on. From the ground station: ssh astronaut-pi (through the Jetson; docs/SETUP.md).
  - Full bench check, including the encoder benchmark: make probe
  - Service state and logs: make status / make logs
EOF
