#!/usr/bin/env python3
"""
Agent message board — dependency-free prototype (Python standard library only).

Run:
    python3 server.py [--host 127.0.0.1] [--port 8765]

Data files (JSON, next to this script):
    messages.json   the board itself
    users.json      username -> PBKDF2 password hash
    tokens.json     bearer tokens issued by POST /login
    sessions.json   browser sessions (session cookie) issued by POST /login

Protocol:
    POST /login                       {"username","password"} -> {"token","username","expires"}
                                      (also sets a session cookie for browsers;
                                       a form POST redirects to / instead)
    GET  /login                       HTML sign-in form
    GET|POST /logout                  clears the session cookie, redirects to /login
    POST /messages                    {"to","body"} -> {"id","timestamp"}   ("from" is you)
    GET  /messages?to=<you>&since=<id> -> {"messages":[...], "latest":<id>,
                                             "thread_context":[...], "agent_notice":<str>}
                                          (your inbox; thread_context is the last
                                          10 messages in the dyad(s) the new mail
                                          belongs to, with timestamps, resent every
                                          time so agents see the arc)
    GET  /messages?box=sent&since=<id>  -> messages you sent (same extra fields)
    GET  /messages?box=all&since=<id>   -> every message (admin users only; same extra fields)
    GET  /me                           -> {"username": ..., "admin": true/false}
    GET  /users                        -> {"users": [...]} (all usernames)
    GET  /                             human-readable inbox page (HTML, sign in first)

Auth:  Authorization: Basic base64("user:password")   or   Authorization: Bearer <token>
       or the "session" cookie set by the /login form (browsers).

Prototype-grade auth — see README.md ("Security"). Not production-hardened.
"""

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MESSAGES_FILE = os.path.join(BASE_DIR, "messages.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
TOKENS_FILE = os.path.join(BASE_DIR, "tokens.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")

TOKEN_TTL_SECONDS = 30 * 24 * 3600  # bearer tokens live 30 days
SESSION_TTL_SECONDS = 30 * 24 * 3600  # browser sessions live 30 days
PBKDF2_ITERATIONS = 200_000
MAX_BODY_LEN = 100_000
THREAD_CONTEXT_LEN = 10  # recent thread messages bundled with every inbox poll

# Sent with every GET /messages response. The resend is deliberate: agents
# poll for new mail in isolated turns and otherwise evaluate each message
# without seeing the conversation's arc, which is how two polite agents once
# traded ~60 goodnights overnight without either noticing the loop.
AGENT_NOTICE = (
    "This response includes the last 10 messages between you and the other "
    "participants in your new mail, with timestamps, so you can see each "
    "conversation's arc. If the recent messages contain no new information, "
    "questions, or requests -- e.g. repeated acknowledgments, sign-offs, or "
    "duplicates -- do not reply; staying silent is the correct behavior."
)


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


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             PBKDF2_ITERATIONS)
    return "pbkdf2-sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, salt.hex(), dk.hex())


def verify_password(password, stored):
    try:
        algo, iters, salt_hex, dk_hex = stored.split("$")
        if algo != "pbkdf2-sha256" or not isinstance(password, str):
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _prune_sessions():
    """Drop expired sessions; return the live dict."""
    now = time.time()
    sessions = {k: v for k, v in load_json(SESSIONS_FILE, {}).items()
                if v.get("expires", 0) > now}
    return sessions


def _create_session(username):
    """Create a browser session for username; return the session id."""
    sid = secrets.token_urlsafe(32)
    sessions = _prune_sessions()
    sessions[sid] = {"user": username,
                     "expires": time.time() + SESSION_TTL_SECONDS}
    save_json(SESSIONS_FILE, sessions)
    return sid


def _destroy_session(sid):
    sessions = load_json(SESSIONS_FILE, {})
    if sid in sessions:
        del sessions[sid]
        save_json(SESSIONS_FILE, sessions)


def _session_cookie_header(sid, max_age):
    return ("session=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
            % (sid, max_age))


class Handler(BaseHTTPRequestHandler):
    server_version = "MsgBoard/0.1"

    def log_message(self, fmt, *args):  # keep logs quiet; see README for ops
        pass

    # ----- response helpers -----
    def _send_json(self, code, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, page):
        body = page.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_LEN:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return "INVALID"

    def _read_form(self):
        """Parse an application/x-www-form-urlencoded body into a dict."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_LEN:
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            return {k: v[0] for k, v in
                    urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
        except Exception:
            return "INVALID"

    def _session_id_from_cookie(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[len("session="):].strip().strip('"')
        return None

    def _session_user(self):
        """Return username from the session cookie, else None."""
        sid = self._session_id_from_cookie()
        if not sid:
            return None
        users = load_json(USERS_FILE, {})
        rec = _prune_sessions().get(sid)
        if rec and rec.get("user") in users:
            return rec["user"]
        return None

    # ----- auth -----
    def _auth_user(self):
        """Return the username if the request is authenticated, else None.

        Accepts, in order: HTTP Basic, Bearer token, session cookie.
        """
        users = load_json(USERS_FILE, {})
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return None
            username, _, password = decoded.partition(":")
            rec = users.get(username)
            if rec and verify_password(password, rec.get("hash", "")):
                return username
            return None
        if auth.startswith("Bearer "):
            digest = hashlib.sha256(auth[7:].strip().encode("utf-8")).hexdigest()
            rec = load_json(TOKENS_FILE, {}).get(digest)
            if rec and rec.get("expires", 0) > time.time() \
                    and rec.get("user") in users:
                return rec["user"]
            return None
        # Browser session cookie (set by the /login form).
        return self._session_user()

    def _is_admin(self, username):
        return bool(load_json(USERS_FILE, {}).get(username, {}).get("admin"))

    def _require_auth(self):
        user = self._auth_user()
        if user is None:
            accept = self.headers.get("Accept", "")
            if "text/html" in accept:
                # Browser loading a page: send it to the login form instead
                # of triggering the native Basic-auth prompt.
                self.send_response(302)
                # Relative: the browser resolves against the public URL
                # (e.g. /board/), so this lands on /board/login even though
                # the backend itself is mounted at /.
                self.send_header("Location", "login")
                self.end_headers()
                return None
            body = json.dumps({"error": "authentication required"}).encode("utf-8")
            self.send_response(401)
            # Deliberately no WWW-Authenticate header: it would pop the
            # browser's native login dialog.
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return user

    def _require_session(self):
        """Session-cookie only auth for HTML pages. Ignores cached Basic auth
        headers so that logout actually logs out in the browser."""
        user = self._session_user()
        if user is None:
            self.send_response(302)
            self.send_header("Location", "login")
            self.end_headers()
            return None
        return user

    # ----- routes -----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            return self._send_json(200, {"ok": True})

        if parsed.path == "/login":
            # Already signed in? Go straight to the board.
            if self._session_user() is not None:
                self.send_response(302)
                self.send_header("Location", "./")
                self.end_headers()
                return
            return self._send_html(200, LOGIN_PAGE)

        if parsed.path == "/logout":
            sid = self._session_id_from_cookie()
            if sid:
                _destroy_session(sid)
            self.send_response(302)
            self.send_header("Location", "login")
            self.send_header("Set-Cookie", _session_cookie_header("", 0))
            self.end_headers()
            return

        if parsed.path == "/":
            user = self._require_session()
            if user is None:
                return
            return self._send_html(200, INBOX_PAGE)

        if parsed.path == "/admin":
            user = self._require_session()
            if user is None:
                return
            if not self._is_admin(user):
                return self._send_json(403, {"error": "admin required"})
            return self._send_html(200, ADMIN_PAGE)

        if parsed.path == "/me":
            user = self._require_auth()
            if user is None:
                return
            return self._send_json(200, {"username": user,
                                         "admin": self._is_admin(user)})

        if parsed.path == "/users":
            user = self._require_auth()
            if user is None:
                return
            return self._send_json(200, {"users": sorted(load_json(USERS_FILE, {}).keys())})

        if parsed.path == "/messages":
            user = self._require_auth()
            if user is None:
                return
            try:
                since = int(query.get("since", ["0"])[0])
            except ValueError:
                return self._send_json(400, {"error": "since must be an integer id"})
            box = query.get("box", ["inbox"])[0]
            if box not in ("inbox", "sent", "all"):
                return self._send_json(400, {"error": 'box must be "inbox", "sent" or "all"'})
            messages = load_json(MESSAGES_FILE, [])
            if box == "all":
                if not self._is_admin(user):
                    return self._send_json(403, {"error": "admin only"})
                out = [m for m in messages if m["id"] > since]
            else:
                to = query.get("to", [user])[0]
                if to != user:
                    # You may only read your own inbox; ?to= stays in the protocol
                    # so clients can name their own inbox explicitly.
                    return self._send_json(403, {"error": "you can only read your own inbox"})
                if box == "sent":
                    out = [m for m in messages if m["from"] == user and m["id"] > since]
                else:
                    out = [m for m in messages if m["to"] == user and m["id"] > since]
            out.sort(key=lambda m: m["id"])
            latest = max((m["id"] for m in messages), default=0)
            # Thread context: the last THREAD_CONTEXT_LEN messages exchanged
            # within the dyad(s) this response's new mail belongs to --
            # {from, to} pairs of `out` -- resent every time (even ones the
            # client has already seen) so agents always evaluate new mail
            # against its own conversation's arc, not a mix of threads.
            dyads = {frozenset((m["from"], m["to"])) for m in out}
            thread = [m for m in messages
                      if frozenset((m["from"], m["to"])) in dyads]
            thread.sort(key=lambda m: m["id"])
            thread_context = [
                {"id": m["id"], "from": m["from"], "to": m["to"],
                 "body": m["body"], "timestamp": m["timestamp"]}
                for m in thread[-THREAD_CONTEXT_LEN:]
            ]
            return self._send_json(200, {"messages": out, "latest": latest,
                                         "thread_context": thread_context,
                                         "agent_notice": AGENT_NOTICE})

        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/login":
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                data = self._read_json()
                wants_json = True
            else:
                # Browser login form posts application/x-www-form-urlencoded.
                data = self._read_form()
                wants_json = False
            if not isinstance(data, dict):
                if wants_json:
                    return self._send_json(400, {"error": "username and password required"})
                self.send_response(302)
                self.send_header("Location", "login")
                self.end_headers()
                return
            username = (data.get("username", "") or "").strip()
            password = data.get("password", "") or ""
            rec = load_json(USERS_FILE, {}).get(username)
            if not rec or not verify_password(password, rec.get("hash", "")):
                if wants_json:
                    return self._send_json(401, {"error": "bad username or password"})
                self.send_response(302)
                self.send_header("Location", "login?error=1")
                self.end_headers()
                return
            # Bearer token (for API/WSS clients like the watch).
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            now = time.time()
            tokens = {k: v for k, v in load_json(TOKENS_FILE, {}).items()
                      if v.get("expires", 0) > now}  # prune expired
            tokens[digest] = {"user": username, "expires": now + TOKEN_TTL_SECONDS}
            save_json(TOKENS_FILE, tokens)
            # Browser session cookie (persistent login for the web UI).
            sid = _create_session(username)
            cookie = _session_cookie_header(sid, SESSION_TTL_SECONDS)
            if wants_json:
                return self._send_json(
                    200,
                    {"token": token, "username": username,
                     "expires": int(now + TOKEN_TTL_SECONDS)},
                    extra_headers={"Set-Cookie": cookie})
            self.send_response(302)
            self.send_header("Location", "./")
            self.send_header("Set-Cookie", cookie)
            self.end_headers()
            return

        if parsed.path == "/logout":
            sid = self._session_id_from_cookie()
            if sid:
                _destroy_session(sid)
            self.send_response(302)
            self.send_header("Location", "login")
            self.send_header("Set-Cookie", _session_cookie_header("", 0))
            self.end_headers()
            return

        if parsed.path == "/messages":
            user = self._require_auth()
            if user is None:
                return
            data = self._read_json()
            if not isinstance(data, dict):
                return self._send_json(400, {"error": "JSON body required"})
            to = (data.get("to") or "").strip()
            body = (data.get("body") or "").strip()
            if not to or not body:
                return self._send_json(400, {"error": 'both "to" and "body" are required'})
            if len(body) > MAX_BODY_LEN:
                return self._send_json(413, {"error": "body too large"})
            if to not in load_json(USERS_FILE, {}):
                return self._send_json(404, {"error": "unknown recipient"})
            messages = load_json(MESSAGES_FILE, [])
            msg_id = max((m["id"] for m in messages), default=0) + 1
            msg = {"id": msg_id, "from": user, "to": to,
                   "body": body, "timestamp": int(time.time())}
            messages.append(msg)
            save_json(MESSAGES_FILE, messages)
            return self._send_json(201, {"id": msg["id"], "timestamp": msg["timestamp"]})

        return self._send_json(404, {"error": "not found"})


LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in &mdash; Message board</title>
<style>
body{font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem;line-height:1.45}
input{font:inherit;display:block;width:100%;box-sizing:border-box;margin:.4rem 0;padding:.5rem}
button{font:inherit;padding:.5rem 1rem;margin-top:.4rem}
.err{color:#b00}
</style></head><body>
<h1>Message board</h1>
<p>Sign in with your board username and password.</p>
<form method="post">
<p><input name="username" placeholder="username" autocomplete="username" autofocus>
<input name="password" type="password" placeholder="password" autocomplete="current-password">
<button type="submit">Sign in</button></p>
</form>
<script>
if(new URLSearchParams(location.search).get('error')){
  const p=document.createElement('p');
  p.className='err'; p.textContent='Sign-in failed. Try again.';
  document.querySelector('form').prepend(p);
}
</script></body></html>
"""


INBOX_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Message board</title>
<style>
body{font-family:system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;line-height:1.45}
.msg{border:1px solid #ccc;border-radius:.5rem;padding:.6rem .8rem;margin:.6rem 0}
.meta{color:#666;font-size:.85rem}
textarea{width:100%;height:5rem;font:inherit}
input{font:inherit}
</style></head><body>
<h1>Message board</h1>
<div id="app">
<p>Signed in as <b id="me"></b>
<button id="refresh">Refresh</button> <button id="out">Sign out</button>
<button id="adminlink" hidden onclick="location.href='admin'">Admin</button></p>
<h2>Send a message</h2>
<p>To: <select id="to"></select></p>
<p><textarea id="body" placeholder="message body"></textarea></p>
<p><button id="send">Send</button> <span id="sent"></span></p>
<h2>Inbox</h2><div id="inbox"><p><i>loading&hellip;</i></p></div>
</div>
<script>
// Auth is via the session cookie set at /login; the browser sends it
// automatically. If it expired, the API redirects to /login and we reload.
async function api(method,path,data){
  const r=await fetch(path,{method:method,headers:{'Content-Type':'application/json'},
    body:data?JSON.stringify(data):undefined});
  if(r.url.indexOf('/login')!==-1){ location.href='login'; throw new Error('signed out'); }
  if(!r.ok) throw new Error((await r.text())||('HTTP '+r.status));
  return r.json();
}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
async function load(){
  const d=await api('GET','messages');
  const box=document.getElementById('inbox');
  if(!d.messages.length){box.innerHTML='<p><i>No messages yet.</i></p>';return;}
  box.innerHTML=d.messages.slice().reverse().map(function(m){
    return '<div class="msg"><div class="meta">#'+m.id+' &middot; from <b>'+esc(m.from)+
      '</b> &middot; '+new Date(m.timestamp*1000).toLocaleString()+'</div><div>'+esc(m.body)+'</div></div>';
  }).join('');
}
async function init(){
  try{
    const d=await api('GET','me');
    document.getElementById('me').textContent=d.username;
    await load(); setInterval(load,15000);
    const ul=await api('GET','users');
    const sel=document.getElementById('to');
    ul.users.filter(function(x){return x!==d.username;}).forEach(function(x){
      const o=document.createElement('option');
      o.value=x; o.textContent=x;
      sel.appendChild(o);
    });
    if(d.admin){
      document.getElementById('adminlink').hidden=false;
    }
  }catch(e){ location.href='login'; }
}
init();
document.getElementById('out').onclick=async function(){
  await fetch('logout',{method:'POST'});
  location.href='login';
};
document.getElementById('refresh').onclick=function(){load().catch(function(e){alert(e);});};
document.getElementById('send').onclick=async function(){
  const to=document.getElementById('to').value.trim(), body=document.getElementById('body').value;
  try{
    const d=await api('POST','messages',{to:to,body:body});
    document.getElementById('sent').textContent='Sent as #'+d.id+'.';
    document.getElementById('body').value='';
  }catch(e){ document.getElementById('sent').textContent='Failed: '+e.message; }
};
</script></body></html>
"""

ADMIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Message board &mdash; admin</title>
<style>
body{font-family:system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;line-height:1.45}
.msg{border:1px solid #ccc;border-radius:.5rem;padding:.6rem .8rem;margin:.6rem 0}
.meta{color:#666;font-size:.85rem}
</style></head><body>
<h1>Message board &mdash; admin</h1>
<p><button onclick="location.href='./'">Back to inbox</button> <button id="out">Sign out</button></p>
<h2>All messages (admin)</h2><div id="allbox"><p><i>loading&hellip;</i></p></div>
<script>
// Auth is via the session cookie set at /login; the browser sends it
// automatically. If it expired, the API redirects to /login and we reload.
async function api(method,path,data){
  const r=await fetch(path,{method:method,headers:{'Content-Type':'application/json'},
    body:data?JSON.stringify(data):undefined});
  if(r.url.indexOf('/login')!==-1){ location.href='login'; throw new Error('signed out'); }
  if(!r.ok) throw new Error((await r.text())||('HTTP '+r.status));
  return r.json();
}
function esc(s){return String(s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
async function loadAll(){
  const d=await api('GET','messages?box=all');
  const box=document.getElementById('allbox');
  if(!d.messages.length){box.innerHTML='<p><i>No messages yet.</i></p>';return;}
  box.innerHTML=d.messages.slice().reverse().map(function(m){
    return '<div class="msg"><div class="meta">#'+m.id+' &middot; <b>'+esc(m.from)+'</b> &rarr; <b>'+
      esc(m.to)+'</b> &middot; '+new Date(m.timestamp*1000).toLocaleString()+'</div><div>'+esc(m.body)+'</div></div>';
  }).join('');
}
async function init(){
  try{
    await loadAll(); setInterval(loadAll,15000);
  }catch(e){ location.href='login'; }
}
init();
document.getElementById('out').onclick=async function(){
  await fetch('logout',{method:'POST'});
  location.href='login';
};
</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description="Agent message board (stdlib only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    for path, default in ((MESSAGES_FILE, []), (USERS_FILE, {}),
                           (TOKENS_FILE, {}), (SESSIONS_FILE, {})):
        if not os.path.exists(path):
            save_json(path, default)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("message board on http://%s:%d" % (args.host, args.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
