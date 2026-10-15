#!/usr/bin/env python3
"""Mint-token-only CLI for the message board (stdlib only).

HTTP messaging is retired (GET/POST /messages return 410 Gone) — all
messaging uses WSS (see README.md). This tool does the one HTTP job
that remains: exchange a username + password for a bearer token via
POST /login. The token is printed once — keep it secret.

Auth: --user/--password (or MB_USER/MB_PASSWORD env vars, or
interactive prompt).

Example:
    python3 client.py --base http://127.0.0.1:8765 mint-token
"""
import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.request


def call(base, method, path, auth=None, data=None):
    url = base.rstrip("/") + path
    headers = {}
    if auth:
        headers["Authorization"] = auth
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = ""
        sys.exit("HTTP %d %s" % (e.code, detail))


def main():
    ap = argparse.ArgumentParser(
        description="message board client (mint-token only)")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--user", default=os.environ.get("MB_USER"))
    ap.add_argument("--password", default=os.environ.get("MB_PASSWORD"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mint-token", help="get a bearer token (keep it secret)")
    args = ap.parse_args()

    user = args.user or input("username: ").strip()
    password = args.password or getpass.getpass("password: ")
    basic = "Basic " + base64.b64encode(
        ("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
    _, out = call(args.base, "POST", "/login", auth=basic,
                  data={"username": user, "password": password})
    print(out["token"])


if __name__ == "__main__":
    main()
