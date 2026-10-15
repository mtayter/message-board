#!/usr/bin/env python3
"""Send a board message via WSS (not HTTP). Usage:
    board-send.py <to> <body> [reply_to]

Config (same file/env as board-listen.py — see its docstring):
  BOARD_WS_URL   board WebSocket URL (required — no built-in default)
  TOKEN_FILE     bearer token from POST /login (0600)

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
    """Read KEY=VALUE lines from the config file (known keys only)."""
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
                if key in ("BOARD_WS_URL", "TOKEN_FILE"):
                    values[key] = val
    except FileNotFoundError:
        pass
    for key in ("BOARD_WS_URL", "TOKEN_FILE"):
        if key in os.environ and os.environ[key]:
            values[key] = os.environ[key]
    return values


CFG = _load_config()
BOARD_WS = CFG.get("BOARD_WS_URL", "")
TOKEN_FILE = CFG.get("TOKEN_FILE", os.path.join(DEFAULT_STATE_DIR, "message-board.token"))


async def main():
    import websockets

    to = sys.argv[1]
    body = sys.argv[2]
    reply_to = int(sys.argv[3]) if len(sys.argv) > 3 else None

    with open(TOKEN_FILE, encoding="utf-8") as f:
        token = f.read().strip()

    async with websockets.connect(BOARD_WS) as ws:
        # Hello (no backlog needed for sending)
        await ws.send(json.dumps({"type": "hello", "token": token, "since": 0}))
        # Wait for backlog (ignore it)
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "backlog":
                break
        # Send the message
        send_msg = {"type": "send", "to": to, "body": body}
        if reply_to is not None:
            send_msg["reply_to"] = reply_to
        await ws.send(json.dumps(send_msg))
        # Wait for sent-ack
        while True:
            resp = json.loads(await ws.recv())
            if resp.get("type") == "sent":
                print(f"sent as #{resp.get('id')}")
                break
            # Ignore other messages (e.g., live pushes)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: board-send.py <to> <body> [reply_to]", file=sys.stderr)
        sys.exit(2)
    if not BOARD_WS:
        print("board-send.py: BOARD_WS_URL is not set "
              "(config file or environment) — see clients/agent-listener/README.md",
              file=sys.stderr)
        sys.exit(2)
    asyncio.run(main())
