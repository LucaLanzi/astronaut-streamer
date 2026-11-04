#!/usr/bin/env bash
# One-time setup for the astronaut Pi: Raspberry Pi 4B, Raspberry Pi OS Lite
# 64-bit (Bookworm) (PLAN.md §3.2).
#
#   sudo ./pi_install_me.sh            install packages, enable SSH, save the
#                                      Jetson hotspot as the Pi's Wi-Fi
#   sudo ./pi_install_me.sh --connect  ...and switch to the hotspot now
#
# Run it while the Pi still has internet (apt needs it). Afterwards the Pi joins
# the hotspot by itself whenever it's in range, at the fixed address PI_HOTSPOT_IP.
# Settings: config/astronaut.env (+ config/astronaut.local.env for the password).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config

CON_NAME="astro-link"
CONNECT=false

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

for arg in "$@"; do
  case "$arg" in
    --connect) CONNECT=true ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

require_root
REAL_USER="${SUDO_USER:-}"
[ "$REAL_USER" = root ] && REAL_USER=""
# Raspberry Pi OS keeps Wi-Fi switched off (rfkill) until a Wi-Fi country is set.
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_wifi_country "$WIFI_COUNTRY" || warn "couldn't set the Wi-Fi country to $WIFI_COUNTRY"
fi
if command -v rfkill >/dev/null 2>&1; then
  rfkill unblock wifi || true
fi
need nmcli
nm_has_wifi "$PI_WIFI_IFACE" || die "no NetworkManager Wi-Fi interface '$PI_WIFI_IFACE' (see 'nmcli device'; set PI_WIFI_IFACE)"
ask_psk

info "Installing packages (needs internet)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ffmpeg v4l-utils alsa-utils iw openssh-server python3
# vcgencmd (throttle/under-voltage flags) is normally preinstalled; the package
# was renamed from libraspberrypi-bin to raspi-utils.
if ! command -v vcgencmd >/dev/null 2>&1; then
  apt-get install -y raspi-utils || apt-get install -y libraspberrypi-bin ||
    warn "vcgencmd isn't available: the Pi's throttle/under-voltage flags won't be reported"
fi

# Pi OS Lite ships SSH turned off unless it was enabled in Raspberry Pi Imager.
# The ground station reaches the Pi through the Jetson (ssh -J), so it needs it.
info "Enabling SSH..."
systemctl enable --now ssh

if [ -n "$REAL_USER" ]; then
  info "Adding $REAL_USER to the video and audio groups (camera and mic access over SSH)..."
  usermod -aG video,audio "$REAL_USER"
fi

info "Saving the hotspot '$HOTSPOT_SSID' as $CON_NAME ($PI_HOTSPOT_IP)..."
if nm_has_connection "$CON_NAME"; then
  nmcli connection delete "$CON_NAME" >/dev/null
fi
# Highest autoconnect priority, so the Pi prefers the hotspot over any lab/home
# network it knows. Fixed address below NetworkManager's DHCP range (.10-.254).
# never-default: the hotspot only carries the stream, so it never takes over
# the Pi's internet route while another network is still connected.
nmcli connection add type wifi ifname "$PI_WIFI_IFACE" con-name "$CON_NAME" \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  ssid "$HOTSPOT_SSID" \
  802-11-wireless.powersave 2 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PSK" \
  ipv4.method manual ipv4.addresses "$PI_HOTSPOT_IP/$HOTSPOT_PREFIX_LEN" \
  ipv4.never-default yes \
  ipv6.method disabled >/dev/null

info "Bench check (encoder benchmark skipped; run ./probe_devices.py for the full check):"
if [ -n "$REAL_USER" ]; then
  sudo -u "$REAL_USER" python3 "$SCRIPT_DIR/probe_devices.py" --no-encode || true
else
  python3 "$SCRIPT_DIR/probe_devices.py" --no-encode || true
fi

if $CONNECT; then
  echo
  warn "switching Wi-Fi to '$HOTSPOT_SSID': if you're connected to this Pi over Wi-Fi, this session will drop"
  nmcli connection up "$CON_NAME" >/dev/null ||
    die "couldn't join '$HOTSPOT_SSID'; is the Jetson's hotspot up and in range? (on the Jetson: jetson/hotspot.sh status)"
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
  - SSH is on. From the ground station: ssh astronaut-pi (through the Jetson; see PLAN.md §3.1).
  - Full bench check, including the encoder benchmark: $SCRIPT_DIR/probe_devices.py
EOF
if [ -n "$REAL_USER" ]; then
  echo "  - Log out and back in once so $REAL_USER's new video/audio groups take effect."
fi
