# Shared helpers for the astronaut-streamer shell scripts. Source it, don't run it.
# shellcheck shell=bash

ASTRO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "'$cmd' not found; install it first"
  done
}

require_root() {
  [ "$(id -u)" -eq 0 ] || die "run this with sudo"
}

# Read config/astronaut.env, then config/astronaut.local.env on top. A variable
# already set in the environment wins over both (`sudo HOTSPOT_CHANNEL=11 ...`),
# matching pi/devices.py's load_config(). Values are read as plain text, never
# executed.
load_config() {
  local file line key value
  local -A from_files=()
  for file in "$ASTRO_ROOT/config/astronaut.env" "$ASTRO_ROOT/config/astronaut.local.env"; do
    [ -f "$file" ] || continue
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line#"${line%%[![:space:]]*}"}"
      case "$line" in '' | '#'*) continue ;; esac
      line="${line#export }"
      key="${line%%=*}"
      value="${line#*=}"
      [ "$key" != "$line" ] || continue
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      if [ "${#value}" -ge 2 ] && { [[ "$value" == \"*\" ]] || [[ "$value" == \'*\' ]]; }; then
        value="${value:1:${#value}-2}"
      fi
      if [ -n "${!key+set}" ] && [ -z "${from_files[$key]:-}" ]; then
        continue  # set in the environment before we started: keep it
      fi
      printf -v "$key" '%s' "$value"
      from_files[$key]=1
    done <"$file"
  done
}

# Make sure HOTSPOT_PSK holds a valid WPA2 password, asking for it if empty.
ask_psk() {
  if [ -z "${HOTSPOT_PSK:-}" ]; then
    [ -t 0 ] || die "HOTSPOT_PSK is empty; set it in config/astronaut.local.env"
    read -rsp "Wi-Fi password for '$HOTSPOT_SSID' (8-63 characters): " HOTSPOT_PSK
    echo
  fi
  local n=${#HOTSPOT_PSK}
  { [ "$n" -ge 8 ] && [ "$n" -le 63 ]; } || die "the hotspot password must be 8-63 characters (got $n)"
}

# True if NetworkManager has a saved connection with this name.
nm_has_connection() {
  nmcli -t -f NAME connection show | grep -qxF "$1"
}

# True if this Wi-Fi interface exists and NetworkManager manages it.
nm_has_wifi() {
  nmcli -t -f DEVICE,TYPE device | grep -qxF "$1:wifi"
}
