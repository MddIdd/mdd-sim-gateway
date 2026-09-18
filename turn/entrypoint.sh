#!/bin/sh
# Close the relay's egress before it can relay anything. The control plane writes both files:
# the ruleset only lets relayed UDP reach the engines' browser-media sockets, and turnserver
# drops to nobody (proc-user) once it has bound its ports, taking NET_ADMIN with it. A ruleset
# that fails to load stops the container rather than starting an unrestricted relay.
set -eu
nft -f /etc/mdd-turn/nftables.conf
exec turnserver -c /etc/mdd-turn/turnserver.conf
