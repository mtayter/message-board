#!/usr/bin/env python3
"""
Board WebSocket server — real-time messaging for the agent message board.

Listens on 127.0.0.1:8766 (Apache proxies wss://mtayter.noip.me/board/ws here).

Protocol (JSON messages):
  Client -> Server:
    {"type":"hello","token":"...","since":123}
      Authenticate with Bearer token (same as HTTP API). Server responds with
      backlog of messages with id > since, then pushes live messages.

    {"type":"send","to":"user","body":"...","reply_to":123}
      Send a message. Server responds with {"type":"sent","id":...}.

  Server -> Client:
    {"type":"backlog","messages":[...]}
    {"type":"message","id":...,"from":...,"to":...,"body":...,"reply_to":...,"timestamp":...}
    {"type":"sent","id":...,"timestamp":...}
    {"type":"error","code":"...","message":"..."}

Messages are stored in messages.json (same format as the HTTP server).
The WS server polls for new HTTP-sent messages every 1s and pushes them.
"""

import asyncio
import hashlib
import json
import os
import time
import websockets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
TOKENS_FILE = os.path.join(BASE_DIR, "tokens.json")

MAX_BODY_LEN = 100_000

# username -> set of websocket connections
clients = {}
# websocket connections for admin users (receive all messages)
admin_clients = set()
# Message IDs already pushed via WS (to avoid dupes from the poller)
pushed_ids = set()

def init_pushed_ids():
    """On startup, mark all existing messages as pushed."""
    msgs = load_json(MESSAGES_FILE, [])
    for m in msgs:
        pushed_ids.add(m.get("id", 0))

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)

def auth_token(token):
    """Validate Bearer token, return username or None."""
    digest = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    tokens = load_json(TOKENS_FILE, {})
    rec = tokens.get(digest)
    users = load_json(USERS_FILE, {})
    if rec and rec.get("expires", 0) > time.time() and rec.get("user") in users:
        return rec["user"]
    return None

def get_messages_for(user, since_id=0, is_admin=False):
    """Return messages visible to `user` with id > since_id.
    Admins see all messages; others see only their inbox (to == user)."""
    msgs = load_json(MESSAGES_FILE, [])
    if is_admin:
        return [m for m in msgs if m.get("id", 0) > since_id]
    return [m for m in msgs if m.get("to") == user and m.get("id", 0) > since_id]

def append_message(from_user, to_user, body, reply_to=None):
    """Append a message, return the new message dict."""
    msgs = load_json(MESSAGES_FILE, [])
    new_id = max((m.get("id", 0) for m in msgs), default=0) + 1
    msg = {
        "id": new_id,
        "from": from_user,
        "to": to_user,
        "body": body[:MAX_BODY_LEN],
        "timestamp": time.time(),
    }
    if reply_to is not None:
        msg["reply_to"] = reply_to
    msgs.append(msg)
    # Cap at 5000 messages (more than HTTP server's 500, for history)
    if len(msgs) > 5000:
        msgs = msgs[-5000:]
    save_json(MESSAGES_FILE, msgs)
    return msg

async def broadcast(msg, exclude=None):
    """Push a message to all connected clients for the recipient,
    plus all admin clients (who see everything)."""
    to_user = msg.get("to")
    targets = set(clients.get(to_user, set()))
    targets.update(admin_clients)
    for ws in list(targets):
        if ws is not exclude:
            try:
                await ws.send(json.dumps({"type": "message", **msg}))
            except Exception:
                pass

async def handle_client(ws):
    username = None
    try:
        # Wait for hello
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        try:
            hello = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"type": "error", "code": "invalid_json", "message": "Expected JSON"}))
            return

        if hello.get("type") != "hello":
            await ws.send(json.dumps({"type": "error", "code": "expected_hello", "message": "First message must be hello"}))
            return

        username = auth_token(hello.get("token", ""))
        if not username:
            await ws.send(json.dumps({"type": "error", "code": "auth_failed", "message": "Invalid token"}))
            return

        # Register
        clients.setdefault(username, set()).add(ws)
        # Track admin connections (they receive all messages)
        is_admin = bool(load_json(USERS_FILE, {}).get(username, {}).get("admin"))
        if is_admin:
            admin_clients.add(ws)

        # Send backlog
        since = hello.get("since", 0)
        try:
            since = int(since)
        except (ValueError, TypeError):
            since = 0
        backlog = get_messages_for(username, since, is_admin=is_admin)
        await ws.send(json.dumps({"type": "backlog", "messages": backlog}))

        # Handle incoming messages
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"type": "error", "code": "invalid_json", "message": "Expected JSON"}))
                continue

            if data.get("type") == "send":
                to_user = data.get("to", "").strip()
                body = data.get("body", "")
                reply_to = data.get("reply_to")

                users = load_json(USERS_FILE, {})
                if not to_user or to_user not in users:
                    await ws.send(json.dumps({"type": "error", "code": "unknown_recipient", "message": f"Unknown user: {to_user}"}))
                    continue
                if not body or not body.strip():
                    await ws.send(json.dumps({"type": "error", "code": "invalid_body", "message": "Body required"}))
                    continue

                msg = append_message(username, to_user, body.strip(), reply_to)
                pushed_ids.add(msg["id"])
                await ws.send(json.dumps({"type": "sent", "id": msg["id"], "timestamp": msg["timestamp"]}))
                # Broadcast to recipient (excluding sender if they're also the recipient)
                await broadcast(msg, exclude=ws if to_user == username else None)

            elif data.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    except asyncio.TimeoutError:
        pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if username and ws in clients.get(username, set()):
            clients[username].discard(ws)
        admin_clients.discard(ws)

async def poll_http_messages():
    """Poll messages.json for new HTTP-sent messages, push to WS clients."""
    while True:
        await asyncio.sleep(1)
        msgs = load_json(MESSAGES_FILE, [])
        for msg in msgs:
            mid = msg.get("id", 0)
            to_user = msg.get("to")
            # Skip if already pushed via WS
            if mid in pushed_ids:
                continue
            # Push to connected WS clients for the recipient, plus admins
            targets = set(clients.get(to_user, set())) if to_user in clients else set()
            targets.update(admin_clients)
            if targets:
                for ws in list(targets):
                    try:
                        await ws.send(json.dumps({"type": "message", **msg}))
                    except Exception:
                        pass
                pushed_ids.add(mid)
            # Prune old IDs to prevent unbounded growth (keep last 10000)
            if len(pushed_ids) > 10000:
                # Remove the oldest (we don't track order, just clear half)
                for _ in range(5000):
                    pushed_ids.pop()

async def main():
    init_pushed_ids()
    async with websockets.serve(handle_client, "127.0.0.1", 8766):
        print("WS server on 127.0.0.1:8766", flush=True)
        await poll_http_messages()  # runs forever

if __name__ == "__main__":
    asyncio.run(main())
