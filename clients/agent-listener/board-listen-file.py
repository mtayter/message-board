#!/usr/bin/env python3
"""
Board WSS listener, file-output variant — for the listener-handler design
(2026-10-26, not yet the board chat's listener until cutover).

One-shot by design, owned by the listener-handler side chat (NOT the
board side chat). The handler spawns it via a background exec, so this
script's stdout IS the handler's wake handoff.

Behaviour:
  - Connects to the board via WebSocket, authenticates, requests backlog
    with since = last seen message ID (same watermark file as the old
    board-listen.py, so cutover does not re-deliver old messages).
  - For every message seen, advances the watermark past it, whether or
    not it is forwarded.
  - Forwards ONLY messages TO slick (msg["to"] == "slick"), except the
    degenerate slick-to-slick case (from == to == "slick"), which is
    skipped to avoid a self-reply loop. Slick's outbound messages to
    others are never forwarded.
  - Each forwarded message is written atomically to
    ~/message-board-messages/<id>.json (temp file + rename, so the board
    chat never reads a partial file), with the full message dict as
    received (id, from, to, body, timestamp, reply_to, location, ...).
  - If at least one file was written, prints the literal word "message"
    and exits. The handler interprets that as: send "message" to the
    board side chat, then restart this script.
  - If no file was written (no new to-slick messages, or an error /
    timeout), exits silently. The handler restarts this script.
  - Token expiry self-heals: if the server answers the hello with an
    auth_failed error and CREDS_FILE is configured, the listener makes
    ONE re-login attempt (POST /login with the credentials in
    CREDS_FILE), stores the fresh token in TOKEN_FILE (and in the
    legacy ~/hooks/state mirror if that file exists), and retries the
    hello once. Attempts are cooldown-stamped (10 minutes) so a
    persistent auth failure cannot turn the respawn cycle into a
    login hammer. Without CREDS_FILE the behaviour is unchanged.
  - NO full-history regrab: if the watermark file is missing at start,
    this script does NOT forward the backlog. It advances the watermark
    to the largest message ID in the backlog and exits silently, so the
    next run waits live. The board side chat already holds the history;
    re-forwarding everything would re-inject it verbatim and risk
    duplicate replies. (If the backlog is empty, the watermark stays
    unset and the next run behaves normally.)

Config (same file / env as board-listen.py, nothing hardcoded):
  Config file: $AGENT_LISTENER_CONFIG, or
               ~/.config/message-board/agent-listener.env
  BOARD_WS_URL, BOARD_URL, TOKEN_FILE, LAST_FILE — as in board-listen.py
  CREDS_FILE — optional path to a JSON file {"username", "password"}
    (0600) used ONLY for the re-login on an expired token. The config
    itself still never holds a password — only this path.
  MESSAGE_BOARD_MESSAGES_DIR — output directory (env only),
    default ~/message-board-messages

The token is read from its file and never printed or logged. Message
files contain board message bodies by design — that is what the board
chat is meant to read.
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
DEFAULT_OUT_DIR = os.path.join(HOME, "message-board-messages")


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
OUT_DIR = os.environ.get("MESSAGE_BOARD_MESSAGES_DIR", DEFAULT_OUT_DIR)
CREDS_FILE = CFG.get("CREDS_FILE", "")
if CFG.get("BOARD_URL"):
    BOARD_URL = CFG["BOARD_URL"]
elif WS_URL:
    BOARD_URL = WS_URL.replace("wss://", "https://").replace("ws://", "http://")
    if BOARD_URL.endswith("/ws"):
        BOARD_URL = BOARD_URL[:-3]
else:
    BOARD_URL = ""
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
    turn the handler's respawn cycle into a login hammer. The token
    and password are never printed or logged.
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


def should_forward(msg):
    """Forward only messages TO slick, excluding slick-to-slick."""
    if msg.get("to") != "slick":
        return False
    if msg.get("from") == "slick" and msg.get("to") == "slick":
        return False
    return True


def write_message_file(msg):
    """Write one message atomically as <id>.json. Returns True if written."""
    msg_id = msg.get("id")
    if msg_id is None:
        return False
    os.makedirs(OUT_DIR, exist_ok=True)
    final_path = os.path.join(OUT_DIR, "%s.json" % (msg_id,))
    if os.path.exists(final_path):
        return False  # Already delivered; do not overwrite / re-deliver.
    tmp_path = final_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(msg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_path, final_path)
    return True


async def _run_once(websockets, token):
    """One connect/hello/run cycle. Returns the server's error dict if
    it rejected the hello, else None."""
    since = read_last()
    watermark_missing = not os.path.exists(LAST_FILE)

    try:
        async with websockets.connect(WS_URL) as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "token": token,
                "since": since,
            }))

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                data = json.loads(raw)
            except asyncio.TimeoutError:
                return None  # No backlog, exit empty; handler restarts us.

            if data.get("type") == "error":
                return data

            if data.get("type") == "backlog":
                msgs = data.get("messages", [])
                if watermark_missing and msgs:
                    # Skip to head, do not forward anything (see header).
                    for msg in msgs:
                        write_last(msg.get("id", since))
                    return None  # Handler restarts us; next run waits live.
                wrote_any = False
                for msg in msgs:
                    write_last(msg.get("id", since))
                    if should_forward(msg):
                        if write_message_file(msg):
                            wrote_any = True
                if wrote_any:
                    print("message", flush=True)
                    return None  # Batch delivered; handler routes and restarts us.
                # No forwarded messages (including empty backlog): fall
                # through to wait live. Returning here would make the
                # handler restart us in a tight loop on every empty start.

            # No backlog messages — wait for live messages indefinitely.
            # Dead peers are caught by the websockets keepalive (20s ping /
            # 20s pong timeout): a dead connection raises, the outer
            # handler exits, and the handler respawns us.
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                if data.get("type") == "message":
                    msg = {k: v for k, v in data.items() if k != "type"}
                    write_last(msg.get("id", since))
                    if should_forward(msg):
                        write_message_file(msg)
                        print("message", flush=True)
                        return None  # Got a live to-slick message, exit.
                    # Message for someone else (should not normally arrive
                    # on slick's connection): keep waiting, watermark kept.
                # Ignore other types.

    except Exception:
        # Don't print errors for empty exits; just return.
        return None


async def listen():
    import websockets

    err = await _run_once(websockets, read_token())
    if err is not None and err.get("code") == "auth_failed" and refresh_token():
        # Token had expired: exactly one retry with the fresh token.
        await _run_once(websockets, read_token())


if __name__ == "__main__":
    if not WS_URL:
        print("board-listen-file.py: BOARD_WS_URL is not set "
              "(config file or environment) — see clients/agent-listener/README.md",
              file=sys.stderr)
        sys.exit(2)
    asyncio.run(listen())
