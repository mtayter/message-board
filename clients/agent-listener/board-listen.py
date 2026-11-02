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
  CREDS_FILE     optional path to a JSON file {"username", "password"}
                 (0600). Used ONLY to re-login once if the server
                 rejects the cached token as expired (auth_failed):
                 the fresh token is stored in TOKEN_FILE (and in the
                 legacy ~/hooks/state mirror if present) and the hello
                 retried once. Attempts are cooldown-stamped (10 min)
                 so a persistent auth failure cannot become a login
                 hammer. The config itself never holds a password.

The token is read from its file and never printed or logged.
"""
import asyncio
import json
import os
import sys
import time
import urllib.request

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
                if key in ("BOARD_WS_URL", "BOARD_URL", "TOKEN_FILE", "LAST_FILE", "CREDS_FILE"):
                    values[key] = val
    except FileNotFoundError:
        pass
    for key in ("BOARD_WS_URL", "BOARD_URL", "TOKEN_FILE", "LAST_FILE", "CREDS_FILE"):
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
CREDS_FILE = CFG.get("CREDS_FILE", "")
LOGIN_STAMP_FILE = TOKEN_FILE + ".lastlogin"
LOGIN_COOLDOWN_SECONDS = 600
MIRROR_TOKEN_FILE = os.path.join(HOME, "hooks", "state", "message-board.token")


def read_token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def _write_token_file(path, token):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, path)


def refresh_token():
    """One re-login attempt after an auth_failed hello.

    Returns True if a fresh token was stored. The attempt is stamped
    (success or failure) and skipped inside the cooldown window, so a
    persistent failure (e.g. the password changed server-side) cannot
    turn the respawn cycle into a login hammer. The token and password
    are never printed or logged.
    """
    if not CREDS_FILE or not BOARD_URL:
        return False
    try:
        if time.time() - os.path.getmtime(LOGIN_STAMP_FILE) < LOGIN_COOLDOWN_SECONDS:
            return False
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(LOGIN_STAMP_FILE), exist_ok=True)
        with open(LOGIN_STAMP_FILE, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass
    try:
        with open(CREDS_FILE, "r", encoding="utf-8") as f:
            creds = json.load(f)
        body = json.dumps({"username": creds["username"],
                           "password": creds["password"]}).encode("utf-8")
        req = urllib.request.Request(
            BOARD_URL.rstrip("/") + "/login", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read().decode("utf-8"))["token"]
    except Exception:
        return False
    _write_token_file(TOKEN_FILE, token)
    try:
        if os.path.exists(MIRROR_TOKEN_FILE):
            _write_token_file(MIRROR_TOKEN_FILE, token)
    except OSError:
        pass
    return True


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


async def _run_once(websockets, token):
    """One connect/hello/run cycle. Returns the server's error dict if
    it rejected the hello, else None."""
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
                return None  # No backlog, exit empty

            if data.get("type") == "error":
                return data

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
                    return None  # Got messages, exit

            # No backlog messages — wait for live messages indefinitely.
            # Dead peers are caught by the websockets keepalive (20s ping /
            # 20s pong timeout): a dead connection raises, the outer
            # handler exits, and the owning side chat respawns us. The
            # hook's VM detection respawns us after a replacement.
            # (The old 2h cap was removed 2026-10-24: it only bought a
            # respawn turn every 2h with no board activity.)
            while True:
                raw = await ws.recv()
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
                    return None  # Got a live message, exit
                # Ignore other types (e.g., backlog already handled)

    except Exception:
        # Don't print errors for empty exits; just return
        return None


async def listen():
    import websockets

    err = await _run_once(websockets, read_token())
    if err is not None and err.get("code") == "auth_failed" and refresh_token():
        # Token had expired: exactly one retry with the fresh token.
        err = await _run_once(websockets, read_token())
    if err is not None:
        print(json.dumps({"_listener_error": err}), flush=True)


if __name__ == "__main__":
    if not WS_URL:
        print("board-listen.py: BOARD_WS_URL is not set "
              "(config file or environment) — see clients/agent-listener/README.md",
              file=sys.stderr)
        sys.exit(2)
    asyncio.run(listen())
