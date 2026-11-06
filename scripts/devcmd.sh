#!/usr/bin/env bash
# Send a command to the debug build's USB console.
#   scripts/devcmd.sh poll | weather | tide | reset | boot | status | help
set -euo pipefail
TTY="${WX_TTY:-/dev/ttyACM0}"
[ $# -eq 1 ] || { echo "usage: $0 poll|weather|tide|reset|boot|status|help" >&2; exit 2; }
printf '%s\r\n' "$1" > "$TTY"
