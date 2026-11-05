#!/usr/bin/env bash
# Flash a UF2 onto the ProMicro/nice!nano without touching the reset button.
#
#   scripts/flash.sh [path/to/zephyr.uf2]     (default: debug build)
#
# If the UF2 drive isn't mounted, ask a running debug build to reboot into the
# bootloader via its USB console ("boot" command; 1200-baud touch as fallback),
# then wait for the drive and copy the image.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UF2="${1:-$ROOT/firmware/build-debug/firmware/zephyr/zephyr.uf2}"
MNT="${UF2_MOUNT:-/run/media/$USER/PROMICRO}"
TTY="${WX_TTY:-/dev/ttyACM0}"

[ -f "$UF2" ] || { echo "no image: $UF2" >&2; exit 1; }

if [ ! -f "$MNT/INFO_UF2.TXT" ]; then
	if [ -c "$TTY" ]; then
		echo "asking device on $TTY to enter bootloader"
		printf 'boot\r\n' > "$TTY" || true
		for _ in $(seq 1 20); do
			[ -f "$MNT/INFO_UF2.TXT" ] && break
			sleep 0.5
		done
		if [ ! -f "$MNT/INFO_UF2.TXT" ] && [ -c "$TTY" ]; then
			echo "no drive yet; trying 1200-baud touch"
			stty -F "$TTY" 1200 || true
		fi
	else
		echo "no $TTY: double-tap reset to enter the bootloader"
	fi
	for _ in $(seq 1 60); do
		[ -f "$MNT/INFO_UF2.TXT" ] && break
		sleep 0.5
	done
fi

[ -f "$MNT/INFO_UF2.TXT" ] || { echo "UF2 drive not found at $MNT" >&2; exit 1; }
sleep 1
cp "$UF2" "$MNT/" && sync
echo "flashed $(basename "$UF2") -> $MNT"
