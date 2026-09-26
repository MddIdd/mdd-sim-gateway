#!/bin/sh
# Start coturn with the control plane's configuration (MDD_RELAY_CONFIG) plus the addresses only
# known once this container is attached:
#   relay-ip      its address on the media network (MDD_MEDIA_SUBNET), where allocations live;
#   listening-ip  every other address, so clients cannot use the relay to reach the relay itself.
set -eu
: "${MDD_RELAY_CONFIG:?}" "${MDD_MEDIA_SUBNET:?}"

addresses=$(hostname -I | tr ' ' '\n' | awk -v subnet="$MDD_MEDIA_SUBNET" '
  function number(address,  part) {
    split(address, part, ".")
    return ((part[1] * 256 + part[2]) * 256 + part[3]) * 256 + part[4]
  }
  BEGIN { split(subnet, s, "/"); first = number(s[1]); size = 2 ^ (32 - s[2]) }
  /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {
    n = number($0)
    if (n >= first && n < first + size) print "relay-ip=" $0
    else print "listening-ip=" $0
  }')
case "$addresses" in
  *relay-ip=*) ;;
  *) echo "mdd-relay: no address on the media network $MDD_MEDIA_SUBNET" >&2; exit 1 ;;
esac

{
  printf '%s\n' "$MDD_RELAY_CONFIG"
  printf '%s\n' "$addresses"
  # The control plane's health check asks over loopback.
  printf 'listening-ip=127.0.0.1\n'
} > /tmp/turnserver.conf
exec turnserver -c /tmp/turnserver.conf
