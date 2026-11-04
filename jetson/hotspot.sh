#!/usr/bin/env bash
# The Jetson's Wi-Fi hotspot for the astronaut Pi (PLAN.md §3.1).
#
#   sudo ./hotspot.sh up       create (or recreate) the hotspot and start it;
#                              it then starts by itself at every boot
#   sudo ./hotspot.sh start    start the saved hotspot
#   sudo ./hotspot.sh stop     stop it (until the next boot or start)
#   sudo ./hotspot.sh remove   delete it
#        ./hotspot.sh status   show the hotspot and what's connected to it
#
# Settings: config/astronaut.env (+ config/astronaut.local.env for the password).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
load_config

CON_NAME="astro-hotspot"
# The configured interface, else the first Wi-Fi device (names differ by
# Jetson model: wlan0, wlP1p1s0, ...).
IFACE="$(wifi_iface "$JETSON_HOTSPOT_IFACE")"

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# `iw list` prints "Supported interface modes:" for each radio; AP must be in it.
supports_ap() {
  iw list 2>/dev/null | awk '
    /Supported interface modes:/ { modes = 1; next }
    modes && /^[[:space:]]+\*/   { if ($2 == "AP") found = 1; next }
    modes                        { modes = 0 }
    END                          { exit !found }'
}

band_name() {
  case "$HOTSPOT_BAND" in bg) echo "2.4 GHz" ;; a) echo "5 GHz" ;; *) echo "band $HOTSPOT_BAND" ;; esac
}

cmd_up() {
  require_root
  need nmcli iw
  [ -n "$IFACE" ] || die "no Wi-Fi interface found (see 'nmcli device'; PLAN.md §8 has the USB adapter fallback)"
  nm_has_wifi "$IFACE" || die "no NetworkManager Wi-Fi interface '$IFACE' (see 'nmcli device'; set JETSON_HOTSPOT_IFACE)"
  supports_ap || die "this Wi-Fi card doesn't list AP mode in 'iw list'; see PLAN.md §8 for the USB adapter fallback"
  command -v dnsmasq >/dev/null 2>&1 || die "NetworkManager needs dnsmasq to hand out addresses: sudo apt install dnsmasq-base"
  ask_psk

  local current
  current="$(nmcli -g GENERAL.CONNECTION device show "$IFACE" 2>/dev/null || true)"
  if [ -n "$current" ] && [ "$current" != "$CON_NAME" ]; then
    warn "$IFACE is connected to '$current'; the hotspot replaces that connection"
  fi

  iw reg set "$WIFI_COUNTRY" 2>/dev/null || warn "couldn't set the Wi-Fi country to $WIFI_COUNTRY"
  nmcli radio wifi on
  if nm_has_connection "$CON_NAME"; then
    nmcli connection delete "$CON_NAME" >/dev/null
  fi

  # WPA2 (RSN + CCMP) only; Wi-Fi power saving off, because it causes periodic
  # latency spikes on a streaming link; `shared` = DHCP + NAT for the hotspot.
  nmcli connection add type wifi ifname "$IFACE" con-name "$CON_NAME" \
    connection.autoconnect yes connection.autoconnect-priority 100 \
    ssid "$HOTSPOT_SSID" \
    802-11-wireless.mode ap \
    802-11-wireless.band "$HOTSPOT_BAND" \
    802-11-wireless.channel "$HOTSPOT_CHANNEL" \
    802-11-wireless.powersave 2 \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn \
    wifi-sec.pairwise ccmp wifi-sec.group ccmp \
    wifi-sec.psk "$HOTSPOT_PSK" \
    ipv4.method shared ipv4.addresses "$JETSON_HOTSPOT_IP/$HOTSPOT_PREFIX_LEN" \
    ipv6.method disabled >/dev/null

  nmcli connection up "$CON_NAME" >/dev/null ||
    die "the hotspot didn't start; check: journalctl -u NetworkManager -n 50"
  info "Hotspot '$HOTSPOT_SSID' is up on $IFACE: $JETSON_HOTSPOT_IP/$HOTSPOT_PREFIX_LEN, $(band_name) channel $HOTSPOT_CHANNEL."
  info "It starts by itself at every boot. The Pi joins it as $PI_HOTSPOT_IP (pi/pi_install_me.sh)."
  echo
  cmd_status
}

cmd_start() {
  require_root
  nm_has_connection "$CON_NAME" || die "no saved hotspot; create it with: sudo $0 up"
  nmcli connection up "$CON_NAME" >/dev/null
  info "Hotspot '$HOTSPOT_SSID' started."
}

cmd_stop() {
  require_root
  nm_has_connection "$CON_NAME" || die "no saved hotspot"
  nmcli connection down "$CON_NAME" >/dev/null || true
  info "Hotspot stopped. It starts again at the next boot, or with: sudo $0 start"
}

cmd_remove() {
  require_root
  if nm_has_connection "$CON_NAME"; then
    nmcli connection delete "$CON_NAME" >/dev/null
    info "Hotspot removed."
  else
    info "No hotspot to remove."
  fi
}

cmd_status() {
  need nmcli
  if ! nm_has_connection "$CON_NAME"; then
    echo "Hotspot: not set up. Create it with: sudo $0 up"
    return 1
  fi
  if ! nmcli -t -f NAME connection show --active | grep -qxF "$CON_NAME"; then
    echo "Hotspot: saved but not running. Start it with: sudo $0 start"
    return 1
  fi
  echo "Hotspot: UP  ssid=$HOTSPOT_SSID  interface=$IFACE  address=$JETSON_HOTSPOT_IP"
  if command -v iw >/dev/null 2>&1; then
    iw dev "$IFACE" info 2>/dev/null | awk '/type|channel|txpower/ { $1 = $1; print "  " $0 }'
    echo "Connected devices:"
    iw dev "$IFACE" station dump 2>/dev/null | awk '
      /^Station/          { mac = $2 }
      /^[[:space:]]+signal:/ { signal = $2 }
      /connected time:/   { printf "  %s  signal %s dBm  connected %s s\n", mac, signal, $3; n++ }
      END                 { if (!n) print "  (none)" }'
  fi
  echo "Addresses seen on the hotspot:"
  ip -4 neigh show dev "$IFACE" 2>/dev/null | awk '{ print "  " $0; n++ } END { if (!n) print "  (none yet)" }'
}

case "${1:-}" in
  up) cmd_up ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  remove) cmd_remove ;;
  status) cmd_status ;;
  -h | --help | help | "") usage ;;
  *) usage >&2; exit 2 ;;
esac
