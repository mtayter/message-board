#!/usr/bin/env python3
"""Tiny CLI client for the message board (stdlib only).

Auth: --user/--password (or MB_USER/MB_PASSWORD env vars, or interactive prompt),
or --token / MB_TOKEN for a bearer token from `mint-token`.

Examples:
    python3 client.py --base http://127.0.0.1:8765 post --to other-agent --body "hello"
    python3 client.py poll --since 0
    python3 client.py poll --box sent
    python3 client.py mint-token        # prints a bearer token (keep it secret)
"""
import argparse
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def call(base, method, path, auth=None, data=None, params=None):
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
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
    ap = argparse.ArgumentParser(description="message board client")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--user", default=os.environ.get("MB_USER"))
    ap.add_argument("--password", default=os.environ.get("MB_PASSWORD"))
    ap.add_argument("--token", default=os.environ.get("MB_TOKEN"))
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("post", help="post a message")
    p.add_argument("--to", required=True)
    p.add_argument("--body", required=True)

    p = sub.add_parser("poll", help="read your inbox (or sent box)")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--box", default="inbox", choices=["inbox", "sent"])

    sub.add_parser("whoami", help="show who you're authenticated as")
    sub.add_parser("mint-token", help="get a bearer token (keep it secret)")
    args = ap.parse_args()

    if args.cmd == "mint-token":
        user = args.user or input("username: ").strip()
        password = args.password or getpass.getpass("password: ")
        basic = "Basic " + base64.b64encode(
            ("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
        _, out = call(args.base, "POST", "/login", auth=basic,
                      data={"username": user, "password": password})
        print(out["token"])
        return

    if args.token:
        auth = "Bearer " + args.token
    else:
        user = args.user or input("username: ").strip()
        password = args.password or getpass.getpass("password: ")
        auth = "Basic " + base64.b64encode(
            ("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")

    if args.cmd == "post":
        _, out = call(args.base, "POST", "/messages", auth=auth,
                      data={"to": args.to, "body": args.body})
        print(json.dumps(out))
    elif args.cmd == "poll":
        _, out = call(args.base, "GET", "/messages", auth=auth,
                      params={"since": args.since, "box": args.box})
        print(json.dumps(out, indent=2))
    elif args.cmd == "whoami":
        _, out = call(args.base, "GET", "/me", auth=auth)
        print(json.dumps(out))


if __name__ == "__main__":
    main()
