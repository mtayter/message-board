#!/usr/bin/env python3
"""Create or update a message-board user.

Password comes from the MB_PASSWORD env var or an interactive prompt.
It is hashed (PBKDF2-HMAC-SHA256) before storage and never printed.

Usage:
    MB_PASSWORD=... python3 add_user.py <username> [--admin]
    python3 add_user.py <username> [--admin]   # prompts for the password
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server import USERS_FILE, hash_password, load_json, save_json


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    admin = "--admin" in sys.argv[1:]
    if len(args) != 1:
        sys.exit("usage: add_user.py <username> [--admin]")
    username = args[0].strip()
    if not username:
        sys.exit("username must not be empty")
    password = os.environ.get("MB_PASSWORD")
    if not password:
        password = getpass.getpass("Password for '%s': " % username)
    if not password:
        sys.exit("password must not be empty")
    users = load_json(USERS_FILE, {})
    users[username] = {"hash": hash_password(password), "admin": admin}
    save_json(USERS_FILE, users)
    print("saved user '%s'%s" % (username, " (admin)" if admin else ""))


if __name__ == "__main__":
    main()
