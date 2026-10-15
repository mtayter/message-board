#!/bin/bash
# Board listener loop — respawns board-listen.py forever.
# Each run is one-shot (connects via WSS, waits for messages, prints them,
# exits). This loop keeps it running persistently.
#
# Canonical spawn: the side chat spawns THIS LOOP once (backgrounded);
# it never respawns the loop itself. The loop only respawns the
# one-shot listener inside it.
#
# Config comes from the environment / agent-listener.env (see
# board-listen.py) — no paths are hardcoded here. The listener is
# resolved relative to this script's directory, so the installed copy
# and a repo checkout both work.
#
# Python selection: AGENT_LISTENER_PYTHON wins if set. Otherwise the
# loop picks the first python that imports `websockets` — system
# python3, then $HOME/.venv/bin/python (the VM's venv, where the
# `websockets` package lives when system python3 lacks it).
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pick_python() {
    if [ -n "${AGENT_LISTENER_PYTHON:-}" ]; then
        printf '%s' "$AGENT_LISTENER_PYTHON"
        return
    fi
    if python3 -c "import websockets" 2>/dev/null; then
        printf '%s' "python3"
        return
    fi
    if [ -x "$HOME/.venv/bin/python" ] \
        && "$HOME/.venv/bin/python" -c "import websockets" 2>/dev/null; then
        printf '%s' "$HOME/.venv/bin/python"
        return
    fi
    printf '%s' "python3"
}

PYTHON_BIN="$(pick_python)"

while true; do
    "$PYTHON_BIN" "$DIR/board-listen.py"
    # Brief pause before respawn (avoid tight loop on errors)
    sleep 2
done
