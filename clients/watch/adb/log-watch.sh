#!/bin/bash
# Stream the watch log filtered to Slick Walk (and Android crashes).
# Use this if the app ever crashes on launch again; Ctrl-C to stop.
set -u

ADB="${ADB:-$(command -v adb 2>/dev/null || echo "$HOME/platform-tools/adb")}"
if [ ! -x "$ADB" ]; then
    read -rp "adb not found. Path to adb: " ADB
fi
[ -x "$ADB" ] || { echo "adb not executable: $ADB" >&2; exit 1; }

SAVED="$HOME/.slickwalk-watch"
# shellcheck disable=SC1090
[ -f "$SAVED" ] && . "$SAVED"

read -rp "Watch IP address [${IP:-}]: " NEW_IP
IP="${NEW_IP:-${IP:-}}"
read -rp "Watch port [${PORT:-}]: " NEW_PORT
PORT="${NEW_PORT:-${PORT:-}}"
if [ -z "$IP" ] || [ -z "$PORT" ]; then
    echo "IP and port are required." >&2
    exit 1
fi
printf 'IP=%q\nPORT=%q\n' "$IP" "$PORT" > "$SAVED"

"$ADB" connect "$IP:$PORT" || exit 1
exec "$ADB" -s "$IP:$PORT" logcat -v time | grep --line-buffered -i "slick\|AndroidRuntime"
