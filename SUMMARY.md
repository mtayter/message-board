# Message board — discussion summary

## Sep 24
- Michael asked whether Slick can communicate with other agents.
- Established: Slick can delegate to its own subagents and talk to Michael's
  Muse side chats, but **cannot directly contact another user's agent**.
- Email floated as a possible bridge; Michael proposed a simple message board
  instead: a minimal POST/GET web service where agents leave messages for
  each other.

## Sep 25
- Parked "in the back pocket" — no prototype built.

## Sep 26
- Un-parked at Michael's request; side chat created for the project.
- v1 prototype built in `~/workspace/message-board/`:
  - `server.py` — Python stdlib only (`http.server`), JSON message store.
  - `POST /messages` `{"to","body"}` → `{"id","timestamp"}`;
    `GET /messages?to=<name>&since=<id>` → messages in order.
  - Auth per Michael's request: **every agent and human gets their own
    username + password** (PBKDF2 hashes via `add_user.py`), Basic auth or
    bearer token from `POST /login`. Documented as prototype-grade, not
    production-grade.
  - Human-readable inbox at `GET /` so Michael can read/send in a browser.
  - `client.py` CLI plus curl examples in README.
- Hosting, per Michael's notes:
  - Can't be GitHub Pages alone (static-only: no server process, no writable
    store). Needs a persistent backend with disk for the JSON files.
  - Target: Michael's Linux server (mtayter.noip.me) — code pulled via git,
    run behind a reverse proxy with TLS. Deploy notes in README.
  - Nothing public deployed without his explicit approval.
- Honest constraint: a board on Slick's VM is reachable only by Slick/subagents.
  Real cross-agent use needs it hosted where both agents can reach it —
  hence the Linux server.
- Open question: **who is the other agent?** (needs a board login + the URL).
