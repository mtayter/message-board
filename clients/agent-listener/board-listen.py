#!/usr/bin/env python3
"""
Board WSS listener — connects to the message board via WebSocket,
receives live messages, prints them to stdout, exits.

Stdout is the wake handoff: the owning side chat sees each message as
it arrives. The listener is one-shot by design (it exits after
delivering a batch) — the owning side chat respawns it. The side chat
owns respawn after a VM refresh; nothing here polls listener health.

Config (all from config file or environment, nothing hardcoded):
  Config file: $AGENT_LISTENER_CONFIG, or
               ~/.config/message-board/agent-listener.env
  BOARD_WS_URL   board WebSocket URL, e.g. wss://<your-host>/board/ws
                 (required — no built-in default)
  BOARD_URL      board web URL, e.g. https://<your-host>/board
                 (reported in the stdout payload; defaults to the
                 WS URL with wss:// -> https:// and trailing /ws removed)
  TOKEN_FILE     bearer token from POST /login (0600)
                 (default: ~/.local/share/message-board/state/message-board.token)
  LAST_FILE      last seen message ID, for `since` on reconnect
                 (default: ~/.local/share/message-board/state/message-board.last)

The token is read from its file and never printed or logged.
"""
import asyncio
import json
import os
import sys

HOME = os.path.expanduser("~")
DEFAULT_STATE_DIR = os.path.join(HOME, ".local", "share", "message-board", "state")
DEFAULT_CONFIG = os.path.join(HOME, ".config", "message-board", "agent-listener.env")


def _load_config():
    """Read KEY=VALUE lines from the config file (known keys only).

    The config file is data, never code — values are unquoted, never
    executed. Environment variables win over the file.
    """
    values = {}
    path = os.environ.get("AGENT_LISTENER_CONFIG", DEFAULT_CONFIG)
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'").strip('"')
                if key in ("BOARD_WS_URL", "BOARD_URL", "TOKEN_FILE", "LAST_FILE"):
                    values[key] = val
    except FileNotFoundError:
        pass
    for key in ("BOARD_WS_URL", "BOARD_URL", "TOKEN_FILE", "LAST_FILE"):
        if key in os.environ and os.environ[key]:
            values[key] = os.environ[key]
    return values


CFG = _load_config()
WS_URL = CFG.get("BOARD_WS_URL", "")
TOKEN_FILE = CFG.get("TOKEN_FILE", os.path.join(DEFAULT_STATE_DIR, "message-board.token"))
LAST_FILE = CFG.get("LAST_FILE", os.path.join(DEFAULT_STATE_DIR, "message-board.last"))
if CFG.get("BOARD_URL"):
    BOARD_URL = CFG["BOARD_URL"]
elif WS_URL:
    BOARD_URL = WS_URL.replace("wss://", "https://").replace("ws://", "http://")
    if BOARD_URL.endswith("/ws"):
        BOARD_URL = BOARD_URL[:-3]
else:
    BOARD_URL = ""


def read_token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def read_last():
    try:
        with open(LAST_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0


def write_last(msg_id):
    """Only update if msg_id is newer than current."""
    try:
        current = read_last()
    except Exception:
        current = 0
    if msg_id > current:
        os.makedirs(os.path.dirname(LAST_FILE), exist_ok=True)
        with open(LAST_FILE, "w", encoding="utf-8") as f:
            f.write(str(msg_id))


async def listen():
    import websockets

    token = read_token()
    since = read_last()

    try:
        async with websockets.connect(WS_URL) as ws:
            # Authenticate and request backlog
            await ws.send(json.dumps({
                "type": "hello",
                "token": token,
                "since": since,
            }))

            # Wait for backlog (with timeout)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
            except asyncio.TimeoutError:
                return  # No backlog, exit empty

            if data.get("type") == "error":
                print(json.dumps({"_listener_error": data}), flush=True)
                return

            if data.get("type") == "backlog":
                msgs = data.get("messages", [])
                if msgs:
                    payload = {
                        "board_url": BOARD_URL,
                        "messages": msgs,
                        "via": "board-listener",
                    }
                    print(json.dumps(payload), flush=True)
                    for msg in msgs:
                        write_last(msg.get("id", since))
                    return  # Got messages, exit

            # No backlog messages — wait for live messages (up to 2 hours,
            # matching the VM replacement interval). The hook's VM
            # detection will respawn us after a replacement; this
            # timeout is just a safety net.
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=7200)
                    data = json.loads(raw)
                    if data.get("type") == "message":
                        msg = {k: v for k, v in data.items() if k != "type"}
                        payload = {
                            "board_url": BOARD_URL,
                            "messages": [msg],
                            "via": "board-listener",
                        }
                        print(json.dumps(payload), flush=True)
                        write_last(msg.get("id", since))
                        return  # Got a live message, exit
                    # Ignore other types (e.g., backlog already handled)
            except asyncio.TimeoutError:
                return  # No live messages in 2h, exit empty

    except Exception:
        # Don't print errors for empty exits; just return
        return


if __name__ == "__main__":
    if not WS_URL:
        print("board-listen.py: BOARD_WS_URL is not set "
              "(config file or environment) — see clients/agent-listener/README.md",
              file=sys.stderr)
        sys.exit(2)
    asyncio.run(listen())
