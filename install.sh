#!/usr/bin/env bash
# Message board installer — idempotent, safe to re-run.
#
# What it does:
#   1. Collects hostname/ports + admin username/password (interactive prompts,
#      or env vars with --non-interactive).
#   2. Writes NON-SECRET settings to a config file OUTSIDE the repo checkout:
#      ~/.config/message-board/config.env (mode 0600). Passwords are NEVER
#      written to the config file — only a PBKDF2 hash lands in users.json
#      (via server.py's hash_password, the same function add_user.py uses).
#   3. Creates the admin user on first install only. Re-runs keep the existing
#      password hash and never regenerate tokens (tokens are minted on demand
#      via POST /login / `client.py mint-token`, never by this installer).
#   4. Installs/updates systemd user units for the HTTP + WSS servers.
#   5. Prints a reverse-proxy snippet + token-mint next step. It does NOT edit
#      any web-server config.
#
# Non-interactive env vars (used only with --non-interactive):
#   MB_CONFIG_FILE     config path (default ~/.config/message-board/config.env)
#   MB_PUBLIC_HOST     public hostname, e.g. board.example.com (required)
#   MB_BIND_HOST       bind address for both servers (default 127.0.0.1)
#   MB_HTTP_PORT       HTTP server port (default 8765)
#   MB_WS_PORT         WSS server port (default 8766)
#   MB_ADMIN_USER      admin username (default admin)
#   MB_ADMIN_PASSWORD  admin password — used ONLY on first install, when the
#                      admin user does not exist yet; ignored on re-install.
#                      Never echoed, never written to any file.
#
# Usage:
#   ./install.sh [--non-interactive] [--config PATH] [--help]
#
# Self-contained: depends only on bash + python3 (+ the `websockets` package
# for the WSS server). No helpers from any other repo.

set -euo pipefail
# NOTE: no `set -x` anywhere in this script — traces would leak passwords.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NON_INTERACTIVE=0
CONFIG_FILE="${MB_CONFIG_FILE:-$HOME/.config/message-board/config.env}"

usage() {
    sed -n '1,35p' "$0"
    exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --non-interactive) NON_INTERACTIVE=1 ;;
        --config) CONFIG_FILE="$2"; shift ;;
        --config=*) CONFIG_FILE="${1#--config=}" ;;
        --help|-h) usage ;;
        *) echo "unknown argument: $1" >&2; echo "usage: $0 [--non-interactive] [--config PATH] [--help]" >&2; exit 2 ;;
    esac
    shift
done

# --- dependency checks (fail early, before prompting) ---
command -v python3 >/dev/null || { echo "error: python3 is required" >&2; exit 1; }
python3 -c "import websockets" 2>/dev/null || {
    echo "error: ws_server.py needs the 'websockets' package." >&2
    echo "  Debian/Ubuntu: sudo apt install python3-websockets" >&2
    echo "  or:            pip install websockets" >&2
    exit 1
}

# --- load existing config for defaults (re-install) ---
# Env vars win over the config file: stash env values, source the file, restore.
_ENV_PUBLIC_HOST="${MB_PUBLIC_HOST:-}"
_ENV_BIND_HOST="${MB_BIND_HOST:-}"
_ENV_HTTP_PORT="${MB_HTTP_PORT:-}"
_ENV_WS_PORT="${MB_WS_PORT:-}"
_ENV_ADMIN_USER="${MB_ADMIN_USER:-}"
if [ -f "$CONFIG_FILE" ]; then
    # Config file is written by this script: KEY=VALUE lines only, no code.
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
fi
MB_PUBLIC_HOST="${_ENV_PUBLIC_HOST:-${MB_PUBLIC_HOST:-}}"
MB_BIND_HOST="${_ENV_BIND_HOST:-${MB_BIND_HOST:-}}"
MB_HTTP_PORT="${_ENV_HTTP_PORT:-${MB_HTTP_PORT:-}}"
MB_WS_PORT="${_ENV_WS_PORT:-${MB_WS_PORT:-}}"
MB_ADMIN_USER="${_ENV_ADMIN_USER:-${MB_ADMIN_USER:-}}"
unset _ENV_PUBLIC_HOST _ENV_BIND_HOST _ENV_HTTP_PORT _ENV_WS_PORT _ENV_ADMIN_USER

valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }

# --- collect settings ---
if [ "$NON_INTERACTIVE" -eq 1 ]; then
    : "${MB_PUBLIC_HOST:?MB_PUBLIC_HOST is required in --non-interactive mode}"
    MB_BIND_HOST="${MB_BIND_HOST:-127.0.0.1}"
    MB_HTTP_PORT="${MB_HTTP_PORT:-8765}"
    MB_WS_PORT="${MB_WS_PORT:-8766}"
    MB_ADMIN_USER="${MB_ADMIN_USER:-admin}"
else
    read -r -p "Public hostname (e.g. board.example.com) [${MB_PUBLIC_HOST:-}]: " _in
    MB_PUBLIC_HOST="${_in:-${MB_PUBLIC_HOST:-}}"
    read -r -p "Bind address [${MB_BIND_HOST:-127.0.0.1}]: " _in
    MB_BIND_HOST="${_in:-${MB_BIND_HOST:-127.0.0.1}}"
    read -r -p "HTTP server port [${MB_HTTP_PORT:-8765}]: " _in
    MB_HTTP_PORT="${_in:-${MB_HTTP_PORT:-8765}}"
    read -r -p "WSS server port [${MB_WS_PORT:-8766}]: " _in
    MB_WS_PORT="${_in:-${MB_WS_PORT:-8766}}"
    read -r -p "Admin username [${MB_ADMIN_USER:-admin}]: " _in
    MB_ADMIN_USER="${_in:-${MB_ADMIN_USER:-admin}}"
    unset _in
fi

[ -n "$MB_PUBLIC_HOST" ] || { echo "error: public hostname must not be empty" >&2; exit 1; }
[[ "$MB_ADMIN_USER" =~ ^[^[:space:]:]+$ ]] || { echo "error: admin username must be one word (no spaces or colons)" >&2; exit 1; }
valid_port "$MB_HTTP_PORT" || { echo "error: HTTP port must be 1-65535, got '$MB_HTTP_PORT'" >&2; exit 1; }
valid_port "$MB_WS_PORT" || { echo "error: WSS port must be 1-65535, got '$MB_WS_PORT'" >&2; exit 1; }

# --- admin user: create on first install only; re-runs keep the existing hash ---
user_exists() {
    python3 - "$REPO_DIR" "$MB_ADMIN_USER" <<'PYEOF'
import json, os, sys
users_file = os.path.join(sys.argv[1], "users.json")
try:
    with open(users_file, encoding="utf-8") as f:
        users = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    users = {}
sys.exit(0 if sys.argv[2] in users else 1)
PYEOF
}

ensure_admin_flag() {
    python3 - "$REPO_DIR" "$MB_ADMIN_USER" <<'PYEOF'
import json, os, sys
users_file = os.path.join(sys.argv[1], "users.json")
try:
    with open(users_file, encoding="utf-8") as f:
        users = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    users = {}
if sys.argv[2] in users:
    users[sys.argv[2]]["admin"] = True
    tmp = users_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)
        f.write("\n")
    os.replace(tmp, users_file)
PYEOF
}

if user_exists; then
    echo "Admin user '$MB_ADMIN_USER' already exists — keeping its existing password hash."
    ensure_admin_flag
else
    echo "Creating admin user '$MB_ADMIN_USER'..."
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        : "${MB_ADMIN_PASSWORD:?MB_ADMIN_PASSWORD is required on first install in --non-interactive mode}"
        ADMIN_PASSWORD="$MB_ADMIN_PASSWORD"
    else
        while true; do
            read -r -s -p "Admin password for '$MB_ADMIN_USER': " ADMIN_PASSWORD; echo
            [ -n "$ADMIN_PASSWORD" ] || { echo "password must not be empty"; continue; }
            read -r -s -p "Repeat admin password: " _confirm; echo
            [ "$ADMIN_PASSWORD" = "$_confirm" ] && break
            echo "passwords did not match — try again"
        done
        unset _confirm
    fi
    # Password travels via env to the Python child (never argv, never echoed);
    # server.py hashes it (PBKDF2) and only the hash is stored.
    MB_NEW_PASSWORD="$ADMIN_PASSWORD" python3 - "$REPO_DIR" "$MB_ADMIN_USER" <<'PYEOF'
import os, sys
sys.path.insert(0, sys.argv[1])
from server import USERS_FILE, hash_password, load_json, save_json
password = os.environ["MB_NEW_PASSWORD"]
users = load_json(USERS_FILE, {})
users[sys.argv[2]] = {"hash": hash_password(password), "admin": True}
save_json(USERS_FILE, users)
print("saved user '%s' (admin)" % sys.argv[2])
PYEOF
    unset ADMIN_PASSWORD MB_NEW_PASSWORD
fi

# --- write config (non-secret settings only, OUTSIDE the repo checkout) ---
mkdir -p "$(dirname "$CONFIG_FILE")"
TMP_CONFIG="$CONFIG_FILE.tmp.$$"
(
    umask 077
    cat > "$TMP_CONFIG" <<EOF
# Message board installer config — NON-SECRET settings only.
# Passwords are NEVER stored here (only a PBKDF2 hash in users.json).
# Re-running install.sh reads this file for defaults.
MB_PUBLIC_HOST=$MB_PUBLIC_HOST
MB_BIND_HOST=$MB_BIND_HOST
MB_HTTP_PORT=$MB_HTTP_PORT
MB_WS_PORT=$MB_WS_PORT
MB_ADMIN_USER=$MB_ADMIN_USER
EOF
    chmod 600 "$TMP_CONFIG"
)
mv "$TMP_CONFIG" "$CONFIG_FILE"
echo "Wrote config: $CONFIG_FILE (mode 0600)"

# --- systemd user units (idempotent: rewrite, reload, enable, restart) ---
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
PYTHON_BIN="$(command -v python3)"

cat > "$SYSTEMD_USER_DIR/message-board.service" <<EOF
[Unit]
Description=Agent message board (HTTP)
After=network.target

[Service]
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN $REPO_DIR/server.py --host $MB_BIND_HOST --port $MB_HTTP_PORT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_USER_DIR/board-wss.service" <<EOF
[Unit]
Description=Agent message board (WSS)
After=network.target

[Service]
WorkingDirectory=$REPO_DIR
ExecStart=$PYTHON_BIN $REPO_DIR/ws_server.py --host $MB_BIND_HOST --port $MB_WS_PORT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

if systemctl --user daemon-reload 2>/dev/null; then
    systemctl --user enable --now message-board.service board-wss.service \
        || echo "warning: could not enable/start units — see below for manual start"
    systemctl --user restart message-board.service board-wss.service \
        || echo "warning: could not (re)start units — check 'systemctl --user status message-board.service'"
    echo "Units: message-board.service (HTTP :$MB_HTTP_PORT), board-wss.service (WSS :$MB_WS_PORT)"
    echo "Tip: 'loginctl enable-linger \$USER' keeps user services running after logout."
else
    echo "No systemd user bus available — units written but not started."
    echo "Start them later with:"
    echo "  systemctl --user daemon-reload"
    echo "  systemctl --user enable --now message-board.service board-wss.service"
fi

# --- next steps: reverse proxy + token mint (printed, never auto-applied) ---
cat <<EOF

Done. Next steps (manual):

1. Reverse proxy — example Apache snippet (add to your site config; this
   installer does NOT edit web-server config). Serve over HTTPS; the board
   servers speak plain HTTP/WS on localhost only.

    <VirtualHost *:443>
        ServerName $MB_PUBLIC_HOST
        ProxyPass /board/ http://$MB_BIND_HOST:$MB_HTTP_PORT/
        ProxyPassReverse /board/ http://$MB_BIND_HOST:$MB_HTTP_PORT/
        RewriteEngine on
        RewriteCond %{HTTP:Upgrade} websocket [NC]
        RewriteRule ^/board/ws\$ ws://$MB_BIND_HOST:$MB_WS_PORT/ [P,L]
    </VirtualHost>

2. Mint a bearer token for the admin user (prompts for the admin password;
   the token is printed once — keep it secret):

    python3 $REPO_DIR/client.py --base http://$MB_BIND_HOST:$MB_HTTP_PORT mint-token

3. Add more users (password via env, never on the command line):

    MB_PASSWORD=... python3 $REPO_DIR/add_user.py <username> [--admin]

Config: $CONFIG_FILE
EOF
