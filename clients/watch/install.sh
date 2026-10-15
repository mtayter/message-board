#!/bin/bash
# Watch-client installer (board-only, no relay).
#
# Collects the board connection + identities the watch app needs and writes
# them to a local config file OUTSIDE the repo checkout:
#   ~/.config/message-board/watch.env (mode 0600)
# Then ./build-watch-v3.sh generates the gitignored Defaults.java from
# that config (board password baked into the APK, never committed).
#
# Two identities, both from config — nothing is hardcoded:
#   BOARD_USERNAME       board user the watch sends AS (self)
#   BOARD_PEER_USERNAME  board user the watch talks TO (peer agent);
#                        replies are played only from peer to self.
#
# Interactive by default; --non-interactive reads everything from
# environment variables (see --help). Safe to re-run: existing config is
# read back for defaults, the keystore is NEVER regenerated once it exists.
#
# This script never enables tracing (no `set -x`): traces would leak the
# board password. Do not add it.
set -euo pipefail
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NON_INTERACTIVE=0
CONFIG_FILE_OVERRIDE=""

usage() {
    cat <<'EOF'
Usage: ./install.sh [--non-interactive] [--config PATH] [--help]

Interactive (default): prompts for each value, showing the saved value from
a previous run (or a built-in default) in brackets. Press Enter to accept.

--non-interactive: no prompts; every required value must come from the
environment (below) or from an existing config file. Missing required
value => error naming the variable.

Environment variables (non-interactive input; also honored interactively
as prompt defaults / silent values for secrets):
  WATCH_CONFIG                config file (default ~/.config/message-board/watch.env)
  SLICK_WALK_CONFIG           same as WATCH_CONFIG (backward compat)
  BOARD_HOST                  board hostname (REQUIRED)
  BOARD_PORT                  board port (default 443)
  BOARD_USE_TLS               true/false (default true)
  BOARD_WS_PATH               board WebSocket path (default /board/ws)
  BOARD_USERNAME              board username the watch sends as (REQUIRED)
  BOARD_PASSWORD              board password (REQUIRED; read with read -s interactively)
  BOARD_PEER_USERNAME         board username of the peer agent (REQUIRED)
  KEYSTORE_PATH               keystore path (default <config-dir>/debug.keystore)
  KEYSTORE_STORE_PASS         keystore store password (default android)
  KEYSTORE_KEY_PASS           keystore key password (default android)
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=1; shift ;;
        --config) CONFIG_FILE_OVERRIDE="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

die() { echo "install.sh: $*" >&2; exit 1; }
note() { echo "install.sh: $*"; }

CONFIG_FILE="${CONFIG_FILE_OVERRIDE:-${WATCH_CONFIG:-${SLICK_WALK_CONFIG:-$HOME/.config/message-board/watch.env}}}"
CONFIG_DIR="$(dirname "$CONFIG_FILE")"
mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
fi

: "${BOARD_HOST:=${BOARD_HOST:-}}"
: "${BOARD_PORT:=${BOARD_PORT:-}}"
: "${BOARD_USE_TLS:=${BOARD_USE_TLS:-}}"
: "${BOARD_WS_PATH:=${BOARD_WS_PATH:-}}"
: "${BOARD_USERNAME:=${BOARD_USERNAME:-}}"
: "${BOARD_PASSWORD:=${BOARD_PASSWORD:-}}"
: "${BOARD_PEER_USERNAME:=${BOARD_PEER_USERNAME:-}}"
: "${KEYSTORE_PATH:=${KEYSTORE_PATH:-}}"
: "${KEYSTORE_STORE_PASS:=${KEYSTORE_STORE_PASS:-}}"
: "${KEYSTORE_KEY_PASS:=${KEYSTORE_KEY_PASS:-}}"

prompt_default() {
    local var="$1" prompt="$2" def="$3" val
    if [ "$NON_INTERACTIVE" = 1 ]; then
        printf -v "$var" '%s' "$def"
        return
    fi
    read -r -p "$prompt [$def]: " val
    printf -v "$var" '%s' "${val:-$def}"
}

prompt_secret() {
    local var="$1" prompt="$2" default="${3:-}" val
    local saved="${!var:-}"
    if [ "$NON_INTERACTIVE" = 1 ]; then
        if [ -n "$saved" ]; then
            return
        fi
        if [ -n "$default" ]; then
            printf -v "$var" '%s' "$default"
            return
        fi
        die "non-interactive mode: $var is required (or set it in an existing config file)"
    fi
    if [ -n "$saved" ]; then
        printf "%s [saved value — Enter to keep, or type a new one]: " "$prompt"
    else
        printf "%s: " "$prompt"
    fi
    IFS= read -r -s val || true
    echo
    if [ -z "$val" ]; then
        if [ -n "$saved" ]; then
            return
        fi
        if [ -n "$default" ]; then
            printf -v "$var" '%s' "$default"
            return
        fi
        die "$var is required"
    fi
    printf -v "$var" '%s' "$val"
}

prompt_default BOARD_HOST "Board hostname" "${BOARD_HOST:-}"
prompt_default BOARD_PORT "Board port" "${BOARD_PORT:-443}"
prompt_default BOARD_USE_TLS "Board uses TLS (true/false)" "${BOARD_USE_TLS:-true}"
prompt_default BOARD_WS_PATH "Board WebSocket path" "${BOARD_WS_PATH:-/board/ws}"
if [ "$NON_INTERACTIVE" = 1 ]; then
    [ -n "${BOARD_USERNAME:-}" ] || die "non-interactive mode: BOARD_USERNAME is required"
    [ -n "${BOARD_PEER_USERNAME:-}" ] || die "non-interactive mode: BOARD_PEER_USERNAME is required"
else
    read -r -p "Board username the watch sends as [${BOARD_USERNAME:-}]: " _u
    BOARD_USERNAME="${_u:-${BOARD_USERNAME:-}}"
    [ -n "$BOARD_USERNAME" ] || die "board username is required"
    read -r -p "Board username of the peer agent [${BOARD_PEER_USERNAME:-}]: " _p
    BOARD_PEER_USERNAME="${_p:-${BOARD_PEER_USERNAME:-}}"
    [ -n "$BOARD_PEER_USERNAME" ] || die "peer username is required"
fi
prompt_secret BOARD_PASSWORD "Board password"
prompt_default KEYSTORE_PATH "Keystore path" "${KEYSTORE_PATH:-$CONFIG_DIR/debug.keystore}"
prompt_secret KEYSTORE_STORE_PASS "Keystore store password" "android"
prompt_secret KEYSTORE_KEY_PASS "Keystore key password" "android"

case "$BOARD_PORT" in ''|*[!0-9]*) die "board port must be a number: $BOARD_PORT" ;; esac
case "${BOARD_USE_TLS,,}" in
    true|1|yes) BOARD_USE_TLS=true ;;
    false|0|no) BOARD_USE_TLS=false ;;
    *) die "board TLS flag must be true/false: $BOARD_USE_TLS" ;;
esac
[ -n "${BOARD_USERNAME:-}" ] || die "board username is required"
[ -n "${BOARD_PASSWORD:-}" ] || die "board password is required"
[ -n "${BOARD_PEER_USERNAME:-}" ] || die "peer username is required"

q() { printf "'%s'" "${1//\'/\'\\\'\'}"; }
CONFIG_TMP="$CONFIG_DIR/.watch.env.tmp.$$"
{
    echo "# Watch client local config — generated by clients/watch/install.sh. DO NOT COMMIT."
    echo "# Lives outside the repo checkout. Mode 0600. Re-running install.sh"
    echo "# reads this file for defaults and never regenerates the keystore."
    echo "BOARD_HOST=$(q "$BOARD_HOST")"
    echo "BOARD_PORT=$(q "$BOARD_PORT")"
    echo "BOARD_USE_TLS=$(q "$BOARD_USE_TLS")"
    echo "BOARD_WS_PATH=$(q "$BOARD_WS_PATH")"
    echo "BOARD_USERNAME=$(q "$BOARD_USERNAME")"
    echo "BOARD_PASSWORD=$(q "$BOARD_PASSWORD")"
    echo "BOARD_PEER_USERNAME=$(q "$BOARD_PEER_USERNAME")"
    echo "KEYSTORE_PATH=$(q "$KEYSTORE_PATH")"
    echo "KEYSTORE_STORE_PASS=$(q "$KEYSTORE_STORE_PASS")"
    echo "KEYSTORE_KEY_PASS=$(q "$KEYSTORE_KEY_PASS")"
} > "$CONFIG_TMP"
chmod 600 "$CONFIG_TMP"
mv "$CONFIG_TMP" "$CONFIG_FILE"
note "wrote $CONFIG_FILE (0600)"

if [ -f "$KEYSTORE_PATH" ]; then
    note "keystore already exists, keeping it: $KEYSTORE_PATH"
else
    if command -v keytool >/dev/null 2>&1; then
        mkdir -p "$(dirname "$KEYSTORE_PATH")"
        keytool -genkeypair -keystore "$KEYSTORE_PATH" -alias androiddebugkey \
            -storepass "$KEYSTORE_STORE_PASS" -keypass "$KEYSTORE_KEY_PASS" \
            -keyalg RSA -keysize 2048 -validity 10950 \
            -dname "CN=Slick Walk,O=Slick Walk,C=US" 2>/dev/null
        chmod 600 "$KEYSTORE_PATH"
        note "generated keystore: $KEYSTORE_PATH (0600)"
    else
        note "WARNING: keytool not found (install a JDK 17 to generate the keystore)."
        note "Re-run install.sh after installing a JDK — it will pick up where it left off."
    fi
fi

echo
echo "Watch client installed."
echo "  config:   $CONFIG_FILE (0600)"
echo "  keystore: $KEYSTORE_PATH"
echo
echo "Next steps:"
echo "  1. Build the watch APK:  ./build-watch-v3.sh"
echo "  2. Install on the watch:  ./adb/update-watch.sh  (or adb install SlickWalk-v3.apk)"
echo "  3. Make sure board accounts exist for both the watch user"
echo "     ($BOARD_USERNAME) and the peer agent ($BOARD_PEER_USERNAME),"
echo "     and the board server is reachable at the configured host."
