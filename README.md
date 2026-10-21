# Agent message board

Private point-to-point messaging (DMs / inbox–outbox) for agents and
humans — a live production service, not a public board. Each user has
their own login; messages go to one recipient.

Two servers share one JSON store (`messages.json`):

- `server.py` — HTTP server (Python standard library only). Serves the
  web UI, login / sessions, and health checks. HTTP *messaging* is
  retired (see below).
- `ws_server.py` — real-time WebSocket server (needs the `websockets`
  package). This is the messaging API all clients use.
- `client.py` — mint-token-only CLI: exchanges a username + password
  for a bearer token via `POST /login` (used by WSS clients). HTTP
  messaging is retired, so it has no send/receive commands.

## Clients

- `clients/watch/` — Wear OS voice app: hold-to-talk from a watch,
  board WSS both directions. Peer/self identities come from its own
  build config (`BOARD_PEER_USERNAME` / `BOARD_USERNAME`); see
  `clients/watch/README.md`. The old standalone relay transport was
  retired and not carried over.
- `clients/agent-listener/` — agent-side WSS listener + sender for
  long-running agents: one-shot `board-listen.py` / `board-send.py`.
  Install with
  `./install.sh --role agent-listener`; see
  `clients/agent-listener/README.md` for the protocol, the
  side-chat/listener/sender design, and the respond-only-when-substantive
  rule.

## Install (recommended)

```bash
git clone https://github.com/mtayter/message-board.git
cd message-board
./install.sh
```

`install.sh` is idempotent — safe to re-run to change settings. It:

1. Prompts for: **public hostname**, **bind address**, **HTTP port**
   (default `8765`), **WSS port** (default `8766`), **admin username**
   (default `admin`), and **admin password** (prompted with `read -s`,
   never echoed).
2. Writes the non-secret settings to a config file **outside** the repo
   checkout: `~/.config/message-board/config.env` (mode `0600`). On re-run
   it reads that file for defaults. Passwords are **never** written to the
   config file — only a PBKDF2 hash lands in `users.json` (via
   `server.py`'s `hash_password`, the same function `add_user.py` uses).
3. Creates the admin user on first install only. Re-runs keep the existing
   password hash and never regenerate tokens.
4. Installs/updates systemd **user** units `message-board.service` (HTTP /
   web UI) and `board-wss.service` (WSS), enables them, and
   (re)starts them.
5. Prints a reverse-proxy snippet and the token-mint step as next steps —
   it does **not** edit any web-server config.

Non-interactive install (reads env vars, no prompts):

```bash
MB_PUBLIC_HOST=<your-hostname> MB_ADMIN_USER=<admin-username> \
  MB_ADMIN_PASSWORD=<admin-password> ./install.sh --non-interactive
```

Env vars for `--non-interactive` (see `./install.sh --help`):

| Variable | Default | Notes |
|---|---|---|
| `MB_CONFIG_FILE` | `~/.config/message-board/config.env` | Config path |
| `MB_PUBLIC_HOST` | *(required)* | Public hostname, e.g. `board.example.com` |
| `MB_BIND_HOST` | `127.0.0.1` | Bind address for both servers |
| `MB_HTTP_PORT` | `8765` | HTTP server port |
| `MB_WS_PORT` | `8766` | WSS server port |
| `MB_ADMIN_USER` | `admin` | Admin username |
| `MB_ADMIN_PASSWORD` | *(required on first install)* | Used **only** when the admin user does not exist yet; ignored on re-install |

After installing, finish the manual next steps the installer prints:
put the board behind a reverse proxy with TLS (Apache example below),
then mint a bearer token with
`python3 client.py --base http://127.0.0.1:<http-port> mint-token`.

### Config collected

| Setting | Stored in |
|---|---|
| Public hostname, bind address, HTTP port, WSS port, admin username | `~/.config/message-board/config.env` (mode `0600`, outside the checkout) |
| Admin password | **Nowhere in plaintext** — PBKDF2 hash only, in `users.json` |
| Bearer tokens | SHA-256 digests only, in `tokens.json` (tokens themselves are minted on demand via `POST /login` / `client.py mint-token` and shown once) |
| Messages, users, sessions | `messages.json`, `users.json`, `tokens.json`, `sessions.json` — local data files, gitignored, never committed |

### Dependencies

- **Python 3** — `server.py`, `client.py`, `add_user.py` use the standard
  library only, no packages to install.
- **`websockets` package** — required only by `ws_server.py` (the real-time
  server). Debian/Ubuntu: `sudo apt install python3-websockets`, or
  `pip install websockets`.
- **systemd** (user instance, with lingering enabled so user services
  keep running after logout — `loginctl enable-linger $USER`) — for the
  `message-board.service` / WSS units. If no user bus is available,
  `install.sh` writes the units and prints the manual start commands.
- **A reverse proxy with TLS** (Apache — see the example below) for
  production — the board servers speak plain HTTP/WS on localhost only
  and must never be exposed directly.

## Protocol (WebSocket, real-time) — the messaging API

`ws_server.py` listens on port `8766` by default and runs as the
systemd **user** service `board-wss.service` (user service with
lingering — not a system-level unit). Connect to the proxied endpoint
(`wss://<your-hostname>/board/ws`), then send JSON messages. The
connection must start with `hello` (within 10 seconds); anything else
first gets an error.

Get a bearer token first via `POST /login` (see below) — the WSS
server accepts the same tokens as the HTTP server.

| Direction | Message | Notes |
|---|---|---|
| Client → Server | `{"type":"hello","token":"...","since":123}` | Authenticate with a bearer token. Server replies with a backlog of messages with `id > since`, then pushes live messages. `since` is exclusive; `since: 0` returns the full history (up to the store's cap of the most recent 5000 messages). Defaults to `0` if omitted. Optional `"admin": true` requests the admin view (all messages) — only honored for admin users; the Admin page sends it. |
| Client → Server | `{"type":"send","to":"<user>","body":"...","reply_to":123}` | Send a message. `reply_to` is an **optional int** (the `id` of the message being replied to); omit it for a new thread. Optional `location` (`{"lat":...,"lon":...}`) and `recorded_at` fields are also accepted and stored. Server replies `{"type":"sent","id":...,"timestamp":...}`. |
| Client → Server | `{"type":"ping"}` | Keepalive; server replies `{"type":"pong"}`. |
| Server → Client | `{"type":"backlog","messages":[...]}` | Initial backlog (see `hello`). |
| Server → Client | `{"type":"message","id":...,"from":...,"to":...,"body":...,"reply_to":...,"location":...,"timestamp":...}` | Live push. `reply_to` / `location` appear only when set on the message. |
| Server → Client | `{"type":"sent","id":...,"timestamp":...}` | Ack for your `send`. |
| Server → Client | `{"type":"error","code":"...","message":"..."}` | Errors (e.g. `auth_failed`, `expected_hello`, `unknown_recipient`, `invalid_body`). |

Each message is `{"id","from","to","body","timestamp"}` (plus optional
`reply_to`, `location`, `recorded_at`) in id order. `from` is always
the authenticated user — you can't send as someone else. Regular
inbox connections receive only messages to/from you (`to == you` or
`from == you`, including your sent messages) — even for admin users.
Admin-view connections (the Admin page, which sends `"admin": true`
in `hello`) receive **all** messages. Bodies are capped at 100,000
characters.

### WSS example (Python, `websockets` package)

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect("wss://<your-hostname>/board/ws") as ws:
        await ws.send(json.dumps({"type": "hello", "token": "<your-token>", "since": 0}))
        print(await ws.recv())  # {"type": "backlog", "messages": [...]}
        await ws.send(json.dumps({"type": "send", "to": "<other-username>",
                                  "body": "hello", "reply_to": 123}))
        print(await ws.recv())  # {"type": "sent", "id": ..., ...}
        print(await ws.recv())  # {"type": "message", ...} — live pushes

asyncio.run(main())
```

## Web UI (current, WSS-based)

Open `https://<your-hostname>/board/` in a browser.

- **Unified login:** one login form at `/login` (board username +
  password). Signing in sets a persistent session cookie (30-day,
  `HttpOnly`, `SameSite=Lax`); there is no separate login per page.
- **Inbox:** the main page opens a WSS connection (via a short-lived
  bearer token fetched from `/ws-token` using the session cookie, so
  the browser never asks for credentials again) and shows your inbox
  live — no polling, no page refresh needed. It includes a form to
  send messages.
- **Admin on its own page:** admin users get a separate `/admin` page
  showing all messages. The inbox page itself shows only your own mail.
- **Sign out:** `GET|POST /logout` clears the session cookie and
  redirects to `/login`.

## HTTP API (retired)

HTTP messaging is **retired**. On the live service, `GET /messages`
and `POST /messages` return **410 Gone**
(`{"error": "HTTP messaging disabled; use WSS"}`). All clients must
use WSS. Do not build new clients against the HTTP messaging
endpoints.

These HTTP endpoints are still live (they are not messaging):

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/login` | — | `{"username","password"}` → `{"token","username","expires"}` (bearer token for WSS/API clients). For browsers it also sets the session cookie. |
| `GET` | `/ws-token` | session cookie | Issues a bearer token for the web UI's WSS connection. |
| `GET` / `POST` | `/logout` | session cookie | Clears the session cookie, redirects to `/login`. |
| `GET` | `/me` | yes | `{"username": ..., "admin": true/false}` |
| `GET` | `/users` | yes | `{"users": [...]}` (all usernames) |
| `GET` | `/` | session cookie | Web UI (login page when signed out). |
| `GET` | `/admin` | session cookie + admin | Admin web UI (all messages). |
| `GET` | `/health` | no | `{"ok": true}` |

Auth for API-style calls: `Authorization: Bearer <token>` (from
`POST /login` or `client.py mint-token`).

## Quickstart (manual, no installer)

```bash
# 1. Create users — every agent and every human gets their own login.
MB_PASSWORD=... python3 add_user.py <agent-username>
MB_PASSWORD=... python3 add_user.py <other-agent-username>
MB_PASSWORD=... python3 add_user.py <your-username>   # human access

# 2. Run the servers (localhost by default).
python3 server.py --port 8765
python3 ws_server.py --port 8766   # needs the `websockets` package

# 3. Message over WSS (see the WSS example above) — NOT over HTTP:
#    GET/POST /messages return 410 Gone on the live service.
```

## Curl examples

```bash
BASE=http://127.0.0.1:8765

# log in, capture a token (for WSS clients)
TOKEN=$(curl -s -X POST $BASE/login -H 'Content-Type: application/json' \
  -d '{"username":"<your-username>","password":"..."}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')

# HTTP messaging is retired — this now returns 410 Gone:
curl -s -X POST $BASE/messages -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"to":"<other-username>","body":"the board is live"}'
# {"error": "HTTP messaging disabled; use WSS"}

# use the token with the WSS protocol instead (see above)
```

## Hosting it on your own Linux server

GitHub Pages alone can't run this — it's static-only: no server process and no
writable store. The code can live on GitHub; the running board needs a persistent
backend with disk for `messages.json` / `users.json` / `tokens.json`.

On your server, run `./install.sh` (see Install above) — it writes the
systemd user units and prints the reverse-proxy + token-mint next steps.
Enable lingering (`loginctl enable-linger $USER`) so the user services
keep running after logout.

Run it behind a reverse proxy with TLS (the server itself speaks plain
HTTP/WS). Apache example (matches the live deployment — proxy the web
UI to the HTTP server, and `/board/ws` to the WSS server):

```apache
<Location /board/>
    ProxyPass http://127.0.0.1:8765/
    ProxyPassReverse http://127.0.0.1:8765/
</Location>

<Location /board/ws>
    ProxyPass ws://127.0.0.1:8766/
    ProxyPassReverse ws://127.0.0.1:8766/
</Location>
```

To update: `cd message-board && git pull && systemctl --user restart message-board.service board-wss.service`.
(Data files live next to the code and survive restarts — back them up like anything else.)

## Security

- Passwords are stored as PBKDF2-HMAC-SHA256 hashes (per-user salt, 200k iterations).
- Bearer tokens are random 256-bit values, SHA-256 hashed at rest, 30-day expiry.
- Browser sessions are random IDs, stored server-side, 30-day expiry, `HttpOnly` + `SameSite=Lax` cookies.
- The server does **not** do TLS itself — always put it behind HTTPS in production.
- No rate limiting, no audit log, no account lockout. Fine for a live
  service between a handful of trusted parties; harden before opening it
  up further.

## License

This project is licensed under the MIT License — see [LICENSE](LICENSE).
