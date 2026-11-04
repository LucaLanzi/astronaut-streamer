#!/usr/bin/env bash
# Addresses from the make command line. The defaults are the rover's
# (Prism side 10.110.0.230, hotspot 10.42.0.1, Pi 10.42.0.2); anything set
# here is saved in config/astronaut.local.env, because the services read it
# every time they start. Always prints the addresses in use.
#
#   make jetson ip rover 192.168.4.97/22      the Jetson's Ethernet (ground-side) address
#   make jetson ip hotspot 192.168.50.1       the Jetson's hotspot address
#   make pi ip 192.168.50.2                   the Pi's own address on the hotspot
#   make pi ip jetson 192.168.50.1            where the Pi finds the Jetson (its hotspot address)
#   make ip                                   show the addresses in use
#   make ip reset                             back to the rover defaults
#
# Several can go in one command: make jetson ip rover 192.168.4.97 ip hotspot 192.168.50.1
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/../config/config.sh
. "$SCRIPT_DIR/../config/config.sh"
report_errors

IP_KEYS=(JETSON_GROUND_IP JETSON_GROUND_PREFIX JETSON_HOTSPOT_IP PI_HOTSPOT_IP HOTSPOT_PREFIX_LEN)

usage() {
  sed -n '7,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

# set_address KEY PREFIX_KEY ADDRESS[/PREFIX]
set_address() {
  valid_ipv4 "$3" || die "'$3' isn't an IPv4 address (like 192.168.4.97 or 192.168.4.97/22)"
  set_local_setting "$1" "${3%/*}"
  if [[ "$3" == */* ]]; then set_local_setting "$2" "${3#*/}"; fi
  changed=true
}

role="${1:-show}"
[ $# -gt 0 ] && shift
changed=false
case "$role" in
  show) [ $# -eq 0 ] || usage ;;
  reset)
    for key in "${IP_KEYS[@]}"; do unset_local_setting "$key"; done
    info "Addresses back to the rover defaults"
    changed=true
    ;;
  jetson | pi)
    while [ $# -gt 0 ]; do
      [ "$1" = ip ] || die "expected 'ip', got '$1' (see: make help)"
      shift
      [ $# -gt 0 ] || usage
      case "$role:$1" in
        jetson:rover | jetson:ground | jetson:eth)
          [ $# -ge 2 ] || usage
          set_address JETSON_GROUND_IP JETSON_GROUND_PREFIX "$2"
          shift 2 ;;
        jetson:hotspot | pi:jetson | pi:hotspot)
          [ $# -ge 2 ] || usage
          set_address JETSON_HOTSPOT_IP HOTSPOT_PREFIX_LEN "$2"
          shift 2 ;;
        pi:pi)
          [ $# -ge 2 ] || usage
          set_address PI_HOTSPOT_IP HOTSPOT_PREFIX_LEN "$2"
          shift 2 ;;
        pi:*)
          set_address PI_HOTSPOT_IP HOTSPOT_PREFIX_LEN "$1"
          shift ;;
        *) die "unknown address '$1' for $role: use 'ip rover <addr>' or 'ip hotspot <addr>'" ;;
      esac
    done
    ;;
  *) usage ;;
esac

load_config
$changed && info "Saved in config/astronaut.local.env (the services use it from their next start)"
echo "Addresses in use (rover defaults unless set; 'make ip reset' restores them):"
printf '  %-34s %s/%s\n' "Jetson Ethernet (ground side)" "$JETSON_GROUND_IP" "$JETSON_GROUND_PREFIX" \
  "Jetson hotspot" "$JETSON_HOTSPOT_IP" "$HOTSPOT_PREFIX_LEN" \
  "Pi on the hotspot" "$PI_HOTSPOT_IP" "$HOTSPOT_PREFIX_LEN"
if ! same_subnet "$JETSON_HOTSPOT_IP" "$PI_HOTSPOT_IP" "$HOTSPOT_PREFIX_LEN"; then
  warn "the Pi ($PI_HOTSPOT_IP) isn't in the hotspot's subnet ($JETSON_HOTSPOT_IP/$HOTSPOT_PREFIX_LEN)."
  warn "Set both to match on each machine, e.g. 'make pi ip ${JETSON_HOTSPOT_IP%.*}.2' (Pi first, then the Jetson)."
fi
if [ "$JETSON_HOTSPOT_IP" = "$PI_HOTSPOT_IP" ]; then
  die "the Jetson's hotspot address and the Pi's address are the same ($PI_HOTSPOT_IP)"
fi
