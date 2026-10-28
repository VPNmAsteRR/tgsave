#!/usr/bin/env python3
"""Авторизация userbot. Код приходит в Telegram, поэтому вход в два шага.

    python3 login.py send --phone +79990000000
    python3 login.py code --code 12345
    python3 login.py code --code 12345 --password '2fa-password'

Пароль и код можно передать через окружение (TG_CODE, TG_PASSWORD), чтобы они
не светились в списке процессов и истории шелла.
"""

import argparse
import asyncio
import json
import os
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

SESSION = os.environ.get("SESSION", "session")
DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/tgsave")
STATE = os.path.join(DATA_DIR, "login.json")


def session_path():
    return SESSION if SESSION.endswith(".session") else f"{SESSION}.session"


def secure(path):
    if os.path.exists(path):
        os.chmod(path, 0o600)


def make_client():
    return TelegramClient(
        SESSION, int(os.environ["API_ID"]), os.environ["API_HASH"], connection_retries=None
    )


def report(me):
    secure(session_path())
    print(f"AUTHORIZED {me.id} @{me.username}")


async def send_code(phone):
    client = make_client()
    await client.connect()
    if await client.is_user_authorized():
        report(await client.get_me())
        await client.disconnect()
        return 0
    sent = await client.send_code_request(phone)
    os.makedirs(DATA_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    with open(STATE, "w") as fh:
        json.dump({"phone": phone, "phone_code_hash": sent.phone_code_hash}, fh)
    os.chmod(STATE, 0o600)
    secure(session_path())
    await client.disconnect()
    print("CODE_SENT")
    return 0


async def submit_password(password):
    client = make_client()
    await client.connect()
    if await client.is_user_authorized():
        report(await client.get_me())
        await client.disconnect()
        return 0
    await client.sign_in(password=password)
    report(await client.get_me())
    try:
        os.remove(STATE)
    except OSError:
        pass
    await client.disconnect()
    return 0


async def submit_code(code, password):
    with open(STATE) as fh:
        state = json.load(fh)
    client = make_client()
    await client.connect()
    try:
        await client.sign_in(state["phone"], code=code, phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        if not password:
            secure(session_path())
            print("NEED_PASSWORD")
            await client.disconnect()
            return 2
        await client.sign_in(password=password)
    report(await client.get_me())
    try:
        os.remove(STATE)
    except OSError:
        pass
    await client.disconnect()
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    p_send = sub.add_parser("send")
    p_send.add_argument("--phone", required=True)
    p_code = sub.add_parser("code")
    p_code.add_argument("--code", default=os.environ.get("TG_CODE"))
    p_code.add_argument("--password", default=os.environ.get("TG_PASSWORD"))
    p_pass = sub.add_parser("password")
    p_pass.add_argument("--password", default=os.environ.get("TG_PASSWORD"))
    args = parser.parse_args()

    if not os.environ.get("API_ID") or not os.environ.get("API_HASH"):
        sys.exit("нужны API_ID и API_HASH")

    if args.mode == "send":
        return asyncio.run(send_code(args.phone))
    if args.mode == "password":
        if not args.password:
            sys.exit("нужен --password или TG_PASSWORD")
        return asyncio.run(submit_password(args.password))
    if not args.code:
        sys.exit("нужен --code или TG_CODE")
    return asyncio.run(submit_code(args.code, args.password))


if __name__ == "__main__":
    sys.exit(main())
