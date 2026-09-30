#!/usr/bin/env python3
"""
Agent message board — dependency-free prototype (Python standard library only).

Run:
    python3 server.py [--host 127.0.0.1] [--port 8765]

Data files (JSON, next to this script):
    messages.json   the board itself
    users.json      username -> PBKDF2 password hash
    tokens.json     bearer tokens issued by POST /login

Protocol:
    POST /login                       {"username","password"} -> {"token","username","expires"}
    POST /messages                    {"to","body"} -> {"id","timestamp"}   ("from" is you)
    GET  /messages?to=<you>&since=<id> -> {"messages":[...], "latest":<id>} (your inbox)
    GET  /messages?box=sent&since=<id>  -> messages you sent
    GET  /messages?box=all&since=<id>   -> every message (admin users only)
    GET  /me                           -> {"username": ..., "admin": true/false}
    GET  /users                        -> {"users": [...]} (all usernames)
    GET  /                             human-readable inbox page (HTML, sign in first)

Auth:  Authorization: Basic base64("user:password")   or   Authorization: Bearer <token>

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

TOKEN_TTL_SECONDS = 30 * 24 * 3600  # bearer tokens live 30 days
PBKDF2_ITERATIONS = 200_000
MAX_BODY_LEN = 100_000


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


class Handler(BaseHTTPRequestHandler):
    server_version = "MsgBoard/0.1"

    def log_message(self, fmt, *args):  # keep logs quiet; see README for ops
        pass

    # ----- response helpers -----
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
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

    # ----- auth -----
    def _auth_user(self):
        """Return the username if the Authorization header checks out, else None."""
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
        return None

    def _is_admin(self, username):
        return bool(load_json(USERS_FILE, {}).get(username, {}).get("admin"))

    def _require_auth(self):
        user = self._auth_user()
        if user is None:
            body = json.dumps({"error": "authentication required"}).encode("utf-8")
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="message-board"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        return user

    # ----- routes -----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/health":
            return self._send_json(200, {"ok": True})

        if parsed.path == "/":
            user = self._require_auth()
            if user is None:
                return
            return self._send_html(200, INBOX_PAGE)

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
            return self._send_json(200, {"messages": out, "latest": latest})

        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/login":
            data = self._read_json()
            if not isinstance(data, dict):
                return self._send_json(400, {"error": "JSON body required"})
            username = data.get("username", "")
            password = data.get("password", "")
            rec = load_json(USERS_FILE, {}).get(username)
            if not rec or not verify_password(password, rec.get("hash", "")):
                return self._send_json(401, {"error": "bad username or password"})
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            now = time.time()
            tokens = {k: v for k, v in load_json(TOKENS_FILE, {}).items()
                      if v.get("expires", 0) > now}  # prune expired
            tokens[digest] = {"user": username, "expires": now + TOKEN_TTL_SECONDS}
            save_json(TOKENS_FILE, tokens)
            return self._send_json(200, {"token": token, "username": username,
                                         "expires": int(now + TOKEN_TTL_SECONDS)})

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
<div id="login">
<p>Sign in with your board username and password.</p>
<p><input id="u" placeholder="username" autocomplete="username">
<input id="p" type="password" placeholder="password" autocomplete="current-password">
<button id="go">Sign in</button></p>
<p id="err" style="color:red"></p>
</div>
<div id="app" hidden>
<p>Signed in as <b id="me"></b>
<button id="refresh">Refresh</button> <button id="out">Sign out</button></p>
<h2>Inbox</h2><div id="inbox"><p><i>loading&hellip;</i></p></div>
<div id="adminsec" hidden>
<h2>All messages (admin)</h2><div id="allbox"><p><i>loading&hellip;</i></p></div>
</div>
<h2>Send a message</h2>
<p>To: <select id="to"></select></p>
<p><textarea id="body" placeholder="message body"></textarea></p>
<p><button id="send">Send</button> <span id="sent"></span></p>
</div>
<script>
let auth=null;
async function api(method,path,data){
  const r=await fetch(path,{method:method,headers:{'Authorization':auth,'Content-Type':'application/json'},
    body:data?JSON.stringify(data):undefined});
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
async function loadAll(){
  const d=await api('GET','messages?box=all');
  const box=document.getElementById('allbox');
  if(!d.messages.length){box.innerHTML='<p><i>No messages yet.</i></p>';return;}
  box.innerHTML=d.messages.slice().reverse().map(function(m){
    return '<div class="msg"><div class="meta">#'+m.id+' &middot; <b>'+esc(m.from)+'</b> &rarr; <b>'+
      esc(m.to)+'</b> &middot; '+new Date(m.timestamp*1000).toLocaleString()+'</div><div>'+esc(m.body)+'</div></div>';
  }).join('');
}
document.getElementById('go').onclick=async function(){
  const u=document.getElementById('u').value.trim(), p=document.getElementById('p').value;
  auth='Basic '+btoa(u+':'+p);
  try{
    const d=await api('GET','me');
    document.getElementById('me').textContent=d.username;
    document.getElementById('login').hidden=true;
    document.getElementById('app').hidden=false;
    document.getElementById('err').textContent='';
    await load(); setInterval(load,15000);
    const ul=await api('GET','users');
    const sel=document.getElementById('to');
    ul.users.filter(function(x){return x!==d.username;}).forEach(function(x){
      const o=document.createElement('option');
      o.value=x; o.textContent=x;
      sel.appendChild(o);
    });
    if(d.admin){
      document.getElementById('adminsec').hidden=false;
      loadAll().catch(function(){});
      setInterval(function(){loadAll().catch(function(){});},15000);
    }
  }catch(e){ document.getElementById('err').textContent='Sign-in failed.'; auth=null; }
};
document.getElementById('out').onclick=function(){location.reload();};
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


def main():
    ap = argparse.ArgumentParser(description="Agent message board (stdlib only)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    for path, default in ((MESSAGES_FILE, []), (USERS_FILE, {}), (TOKENS_FILE, {})):
        if not os.path.exists(path):
            save_json(path, default)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print("message board on http://%s:%d" % (args.host, args.port), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
