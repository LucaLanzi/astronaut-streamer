# Shared helpers for the astronaut-streamer shell scripts. Source it, don't run it.
# shellcheck shell=bash

ASTRO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Per-machine settings (git-ignored). Tests point this somewhere else.
ASTRO_LOCAL_ENV="${ASTRO_LOCAL_ENV:-$ASTRO_ROOT/config/astronaut.local.env}"

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

# For the installers (which run with `set -e`): if a command fails
# unexpectedly, say which one and where instead of stopping silently.
report_errors() {
  set -E
  # shellcheck disable=SC2016,SC2154  # expanded (and rc, src set) when the trap fires
  trap 'rc=$?; src=${BASH_SOURCE[0]:-$0}; printf "error: %s line %s: \"%s\" failed (exit %s)\n" "${src##*/}" "$LINENO" "$BASH_COMMAND" "$rc" >&2' ERR
}

# The packages among "$@" that aren't installed yet, one per line.
missing_packages() {
  local pkg
  for pkg in "$@"; do
    # shellcheck disable=SC2016  # ${Status} is dpkg-query's field name
    dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || echo "$pkg"
  done
}

# apt-get install "$@", but only when something is missing, so re-running an
# installer works offline (in the field) once everything is in place.
apt_install() {
  local missing
  mapfile -t missing < <(missing_packages "$@")
  if [ "${#missing[@]}" -eq 0 ]; then
    info "Packages already installed (no internet needed)"
    return 0
  fi
  info "Installing ${missing[*]} (needs internet)..."
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

# Jetson: make sure its Ethernet has the ground-side address $1/$2 (interface
# $3, or the connected Ethernet). Only ever ADDS it next to the addresses
# already there (DHCP or static stay), so it can't cut off an SSH session, and
# refuses an address another device already answers on (like the Pi's).
ensure_ground_address() {
  local ip="$1" dev con addr="$1/$2"
  valid_ipv4 "$addr" || die "JETSON_GROUND_IP/JETSON_GROUND_PREFIX isn't a valid address: $addr"
  if [ -n "$(iface_with_ip "$ip")" ]; then
    info "Ground-side address: $ip is already on $(iface_with_ip "$ip")"
    return 0
  fi
  if ping -c 2 -W 1 "$ip" >/dev/null 2>&1; then
    die "another device already answers on $ip: pick the Jetson's own address (make jetson ip rover <addr>)"
  fi
  dev="${3:-$(nmcli -t -f DEVICE,TYPE,STATE device | awk -F: '$2 == "ethernet" && $3 == "connected" { print $1; exit }')}"
  if [ -z "$dev" ]; then
    warn "no connected Ethernet to put $addr on: plug it in, then re-run 'make jetson'"
    return 0
  fi
  con="$(nmcli -g GENERAL.CONNECTION device show "$dev" 2>/dev/null || true)"
  if [ -z "$con" ]; then
    warn "$dev has no active NetworkManager connection: can't add $addr"
    return 0
  fi
  info "Ground-side address: adding $addr to $dev ('$con'), next to its current addresses"
  nmcli connection modify "$con" +ipv4.addresses "$addr"
  # With DHCP, keep the connection up even where no DHCP server answers (the Prism link).
  if [ "$(nmcli -g ipv4.method connection show "$con")" = auto ]; then
    nmcli connection modify "$con" ipv4.dhcp-timeout infinity
  fi
  nmcli device reapply "$dev" >/dev/null ||
    warn "couldn't apply it live; it takes effect at the next reboot (or: sudo nmcli connection up '$con')"
  sleep "${ASTRO_SETTLE_S:-2}"   # let the address come up before it's looked for
  info "  (to undo: sudo nmcli connection modify '$con' -ipv4.addresses $addr)"
}

# True if something listens on port $2 ($1 = tcp or udp).
port_in_use() {
  local flag=-t
  if [ "$1" = udp ]; then flag=-u; fi
  ss -H -l -n "$flag" "sport = :$2" 2>/dev/null | grep -q .
}

# The program listening on port $2 ($1 = tcp or udp); empty if none or if
# it can't be seen. Never fails, so it's safe in `x="$(port_owner ...)"`
# under `set -euo pipefail`.
port_owner() {
  local flag=-t
  if [ "$1" = udp ]; then flag=-u; fi
  ss -H -l -n -p "$flag" "sport = :$2" 2>/dev/null | grep -o 'users:(("[^"]*"' | head -1 | cut -d'"' -f2 || true
}

# Read config/astronaut.env, then config/astronaut.local.env on top. A variable
# already set in the environment wins over both (`sudo HOTSPOT_CHANNEL=11 ...`),
# matching pi/devices.py's load_config(). Values are read as plain text, never
# executed.
load_config() {
  local file line key value
  local -A from_files=()
  for file in "$ASTRO_ROOT/config/astronaut.env" "$ASTRO_LOCAL_ENV"; do
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

# Save KEY=value in the per-machine settings file (replacing an existing line).
set_local_setting() {
  local key="$1" value="$2" tmp
  touch "$ASTRO_LOCAL_ENV"
  tmp="$(mktemp)"
  grep -v "^${key}=" "$ASTRO_LOCAL_ENV" >"$tmp" || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  cat "$tmp" >"$ASTRO_LOCAL_ENV"
  rm -f "$tmp"
}

# Remove KEY from the per-machine settings file (back to astronaut.env's value).
unset_local_setting() {
  local key="$1" tmp
  [ -f "$ASTRO_LOCAL_ENV" ] || return 0
  tmp="$(mktemp)"
  grep -v "^${key}=" "$ASTRO_LOCAL_ENV" >"$tmp" || true
  cat "$tmp" >"$ASTRO_LOCAL_ENV"
  rm -f "$tmp"
}

# True if $1 is a dotted IPv4 address, optionally with /prefix (0-32).
valid_ipv4() {
  local addr="${1%/*}" prefix="" octet
  [[ "$1" == */* ]] && prefix="${1#*/}"
  [[ "$addr" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for octet in "${BASH_REMATCH[@]:1}"; do
    [ "$octet" -le 255 ] || return 1
  done
  [ -z "$prefix" ] || { [[ "$prefix" =~ ^[0-9]{1,2}$ ]] && [ "$prefix" -le 32 ]; }
}

# The 32-bit number for a dotted IPv4 address.
ipv4_to_int() {
  local IFS=. a b c d
  read -r a b c d <<<"$1"
  echo $(((a << 24) | (b << 16) | (c << 8) | d))
}

# True if addresses $1 and $2 are in the same /$3 subnet.
same_subnet() {
  local mask=$(((0xFFFFFFFF << (32 - $3)) & 0xFFFFFFFF))
  [ $(($(ipv4_to_int "$1") & mask)) -eq $(($(ipv4_to_int "$2") & mask)) ]
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

# The Wi-Fi interface to use: the configured one, else the first Wi-Fi
# device NetworkManager knows (wlan0 on a Pi, wlP1p1s0 on an Orin Nano, ...).
wifi_iface() {
  local configured="$1"
  if [ -n "$configured" ]; then
    echo "$configured"
  elif command -v nmcli >/dev/null 2>&1; then
    nmcli -t -f DEVICE,TYPE device | awk -F: '$2 == "wifi" { print $1; exit }'
  fi
}

# The interface that holds IPv4 address $1 (empty if none does).
iface_with_ip() {
  ip -o -4 addr show 2>/dev/null | awk -v ip="$1" '{ split($4, a, "/"); if (a[1] == ip) { print $2; exit } }'
}

# Tested MediaMTX release, and the SHA-256 of each download.
PINNED_MTX_VERSION=v1.21.3
declare -A PINNED_MTX_SHA=(
  [arm64]=27f038237cb5f77a724f6afc58eca2798f2bc4edf9989895631a80eeb41a4fe2
  [amd64]=0a2ea7eba96023631efa1bea7804823a9145b043b6646a8a05798d4ac24ed635
)

# MediaMTX's name for this machine's CPU architecture.
mediamtx_arch() {
  case "$(uname -m)" in
    aarch64 | arm64) echo arm64 ;;
    x86_64 | amd64) echo amd64 ;;
    *) return 1 ;;
  esac
}

# Download the MediaMTX release (once) into vendor/, check its SHA-256, and
# print the tarball's path. Later runs reuse the saved copy, so they work
# offline. Messages go to stderr; stdout is just the path.
fetch_mediamtx() {
  local version="${MEDIAMTX_VERSION:-$PINNED_MTX_VERSION}" arch file cache url sha
  arch="$(mediamtx_arch)" || die "unsupported CPU architecture: $(uname -m)"
  file="mediamtx_${version}_linux_${arch}.tar.gz"
  cache="$ASTRO_ROOT/vendor"
  url="https://github.com/bluenviron/mediamtx/releases/download/${version}/${file}"
  mkdir -p "$cache"
  if [ -f "$cache/$file" ]; then
    info "Using the saved copy: vendor/$file" >&2
  else
    info "Downloading MediaMTX $version ($arch)..." >&2
    curl -fL --retry 3 -o "$cache/$file.part" "$url" >&2 || die "download failed: $url"
    mv "$cache/$file.part" "$cache/$file"
  fi
  if [ "$version" = "$PINNED_MTX_VERSION" ]; then
    sha="${PINNED_MTX_SHA[$arch]}"
  else
    warn "MEDIAMTX_VERSION=$version isn't the tested $PINNED_MTX_VERSION; checking it against the release's checksums"
    sha="$(curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${version}/checksums.sha256" |
      awk -v f="$file" '$2 == f || $2 == "*" f { print $1 }')"
    [ -n "$sha" ] || die "no published checksum for $file"
  fi
  if ! echo "$sha  $cache/$file" | sha256sum -c --quiet - >/dev/null 2>&1; then
    rm -f "$cache/$file"
    die "checksum mismatch for $file: the download was removed; re-run to try again"
  fi
  echo "$cache/$file"
}

# Copy template $1 to $2, replacing each @NAME@ with the value of shell
# variable NAME. Fails if a placeholder has no value, so a typo can't
# silently produce a broken config.
render_template() {
  local src="$1" dst="$2" text name value
  shopt -u patsub_replacement 2>/dev/null || true  # bash 5.2: keep '&' literal
  text="$(<"$src")"
  while [[ "$text" =~ @([A-Z_][A-Z0-9_]*)@ ]]; do
    name="${BASH_REMATCH[1]}"
    [ -n "${!name+set}" ] || die "$src: no value for @$name@"
    value="${!name}"
    text="${text//@"$name"@/$value}"
  done
  printf '%s\n' "$text" >"$dst"
}
