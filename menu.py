#!/usr/bin/env python3
"""Меню-бот: инлайн-навигация по архиву, который собирает userbot.

Бот не хранит сообщения — он только читает журнал `saved.db` и рисует
инлайн-клавиатуру: список отправителей → список их сообщений → конкретное
сообщение. Отвечает только владельцу (OWNER_ID), остальным — отказ.

Отправляется через Bot API long polling, поэтому нужен только BOT_TOKEN.
"""

import asyncio
import html
import os
import sqlite3
import sys
import time

import httpx

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
DB_PATH = os.environ.get("DB_PATH", "/var/lib/tgsave/saved.db")

PAGE_SENDERS = 6
PAGE_MESSAGES = 8
TEXT_LIMIT = 3500

MEDIA_METHOD = {
    "photo": "sendPhoto",
    "video": "sendVideo",
    "animation": "sendAnimation",
    "voice": "sendVoice",
    "audio": "sendAudio",
    "document": "sendDocument",
    "video_note": "sendVideoNote",
}

ICONS = {
    "photo": "🖼",
    "video": "🎬",
    "voice": "🎤",
    "video_note": "⭕️",
    "audio": "🎵",
    "sticker": "🩹",
    "gif": "🎞",
    "document": "📎",
    "poll": "📊",
    "location": "📍",
    "text": "💬",
}


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


def esc(value):
    return html.escape(value or "")


def stamp(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Store:
    """Только чтение журнала userbot-а."""

    def __init__(self, path):
        self.db = sqlite3.connect(
            path, check_same_thread=False, timeout=5, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, copy_id INTEGER, chat_id INTEGER, "
            "path TEXT, error TEXT, ts INTEGER)"
        )
        self.db.execute(
            "DELETE FROM requests WHERE ts < ?", (int(time.time()) - 600,)
        )
        self.db.commit()

    def add_request(self, copy_id, chat_id):
        cur = self.db.execute(
            "INSERT INTO requests (copy_id, chat_id, ts) VALUES (?,?,?)",
            (copy_id, chat_id, int(time.time())),
        )
        self.db.commit()
        return cur.lastrowid

    def request(self, request_id):
        return self.db.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()

    def drop_request(self, request_id):
        self.db.execute("DELETE FROM requests WHERE id=?", (request_id,))
        self.db.commit()

    def name(self, tag):
        row = self.db.execute("SELECT name, username FROM senders WHERE tag=?", (tag,)).fetchone()
        return (row["name"], row["username"]) if row else (tag, None)

    def senders(self):
        return self.db.execute(
            """
            SELECT s.tag, s.name, s.username,
                   COUNT(v.message_id) AS n, MAX(v.ts) AS last
            FROM senders s
            JOIN saved v ON v.tag = s.tag AND v.status = 'sent'
            GROUP BY s.tag
            ORDER BY last DESC
            """
        ).fetchall()

    def count(self, tag):
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM saved WHERE tag=? AND status='sent'", (tag,)
        ).fetchone()
        return row["n"]

    def messages(self, tag, limit, offset):
        return self.db.execute(
            """
            SELECT message_id, ts, kind, text, copy_id FROM saved
            WHERE tag=? AND status='sent'
            ORDER BY ts DESC, message_id DESC
            LIMIT ? OFFSET ?
            """,
            (tag, limit, offset),
        ).fetchall()

    def message(self, tag, message_id):
        return self.db.execute(
            "SELECT message_id, ts, kind, text, copy_id FROM saved "
            "WHERE tag=? AND message_id=? AND status='sent'",
            (tag, message_id),
        ).fetchone()

    def totals(self):
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM saved WHERE status='sent'"
        ).fetchone()
        return row["n"]


class Api:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=10.0))

    async def call(self, method, **params):
        response = await self.http.post(f"{self.base}/{method}", json=params)
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]

    async def download(self, file_id):
        """Скачивает файл бота (лимит Bot API — 20 МБ)."""
        try:
            info = await self.call("getFile", file_id=file_id)
        except RuntimeError as exc:
            log("getFile failed:", exc)
            return None
        url = f"https://api.telegram.org/file/bot{TOKEN}/{info['file_path']}"
        response = await self.http.get(url)
        if response.status_code != 200:
            log("download failed:", response.status_code)
            return None
        return response.content

    async def upload(self, method, field, chat_id, data, caption, filename):
        response = await self.http.post(
            f"{self.base}/{method}",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={field: (filename, data)},
        )
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"{method}: {payload.get('description')}")
        return payload["result"]

    async def close(self):
        await self.http.aclose()


def sender_label(row):
    name = row["name"] or "?"
    if row["username"]:
        return f"{name} · @{row['username']}"
    return name


def menu_text(store):
    senders = store.senders()
    return (
        "📂 <b>Архив сообщений</b>\n"
        f"Копий: {store.totals()} · отправителей: {len(senders)}\n\n"
        "Выбери, чьи сообщения показать:"
    )


def menu_markup(store, page):
    senders = store.senders()
    pages = max((len(senders) + PAGE_SENDERS - 1) // PAGE_SENDERS, 1)
    page = min(max(page, 0), pages - 1)
    chunk = senders[page * PAGE_SENDERS : (page + 1) * PAGE_SENDERS]

    rows = [
        [
            {
                "text": f"👤 {clip(sender_label(row), 40)} · {row['n']}",
                "callback_data": f"s|{row['tag']}|0",
            }
        ]
        for row in chunk
    ]
    if pages > 1:
        rows.append(
            [
                {"text": "◀️", "callback_data": f"m|{page - 1}"},
                {"text": f"{page + 1}/{pages}", "callback_data": f"m|{page}"},
                {"text": "▶️", "callback_data": f"m|{page + 1}"},
            ]
        )
    rows.append([{"text": "🔄 Обновить", "callback_data": f"m|{page}"}])
    return {"inline_keyboard": rows}


def sender_text(store, tag, page):
    total = store.count(tag)
    pages = max((total + PAGE_MESSAGES - 1) // PAGE_MESSAGES, 1)
    page = min(max(page, 0), pages - 1)
    rows = store.messages(tag, PAGE_MESSAGES, page * PAGE_MESSAGES)

    name, username = "?", None
    for sender in store.senders():
        if sender["tag"] == tag:
            name, username = sender["name"] or "?", sender["username"]
            break

    head = f"👤 <b>{esc(name)}</b>"
    if username:
        head += f" · @{esc(username)}"
    head += f"\nВсего: {total} · стр. {page + 1}/{pages}\n"

    lines = []
    start = page * PAGE_MESSAGES
    for index, row in enumerate(rows, start=1):
        icon = ICONS.get(row["kind"], "💬")
        body = clip(row["text"], 60) or f"<i>{esc(row['kind'])}</i>"
        lines.append(f"{start + index}. {stamp(row['ts'])} {icon} {esc(body)}")
    body = "\n".join(lines) if lines else "<i>пусто</i>"

    buttons = [
        {
            "text": str(start + index),
            "callback_data": f"d|{tag}|{row['message_id']}|{page}",
        }
        for index, row in enumerate(rows, start=1)
    ]
    keyboard = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    if pages > 1:
        keyboard.append(
            [
                {"text": "◀️", "callback_data": f"s|{tag}|{page - 1}"},
                {"text": f"{page + 1}/{pages}", "callback_data": f"s|{tag}|{page}"},
                {"text": "▶️", "callback_data": f"s|{tag}|{page + 1}"},
            ]
        )
    keyboard.append([{"text": "↩️ К отправителям", "callback_data": "m|0"}])
    return f"{head}\n{body}", {"inline_keyboard": keyboard}


def detail_text(row, tag, page):
    icon = ICONS.get(row["kind"], "💬")
    head = f"{icon} <b>{esc(row['kind'])}</b> · {stamp(row['ts'])}"
    text = esc(row["text"] or "").strip()
    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT] + "…"
    body = text or (
        "<i>без текста — медиа отправляю отдельным сообщением</i>"
        if row["kind"] != "text"
        else "<i>пусто</i>"
    )
    keyboard = {
        "inline_keyboard": [[{"text": "↩️ К списку", "callback_data": f"s|{tag}|{page}"}]]
    }
    return f"{head}\n\n{body}", keyboard


class Menu:
    def __init__(self, api, store):
        self.api = api
        self.store = store

    async def show_menu(self, chat_id, message_id=None, page=0):
        text = menu_text(self.store)
        markup = menu_markup(self.store, page)
        await self.render(chat_id, message_id, text, markup)

    async def render(self, chat_id, message_id, text, markup):
        if message_id:
            try:
                await self.api.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
                return
            except RuntimeError as exc:
                if "not modified" in str(exc):
                    return
        await self.api.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    async def on_message(self, message):
        if message.get("from", {}).get("id") != OWNER_ID:
            return

        text = (message.get("text") or "").strip().lower()
        if text.startswith("/start") or text.startswith("/menu"):
            await self.show_menu(message["chat"]["id"])

    async def on_callback(self, callback):
        callback_id = callback["id"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]

        if callback.get("from", {}).get("id") != OWNER_ID:
            await self.api.call(
                "answerCallbackQuery", callback_query_id=callback_id, text="Нет доступа"
            )
            return

        parts = (callback.get("data") or "").split("|")
        kind = parts[0] if parts else ""

        row = None
        if kind == "m":
            page = int(parts[1]) if len(parts) > 1 else 0
            text = menu_text(self.store)
            markup = menu_markup(self.store, page)
        elif kind == "s" and len(parts) >= 3:
            text, markup = sender_text(self.store, parts[1], int(parts[2]))
        elif kind == "d" and len(parts) >= 4:
            tag, page = parts[1], int(parts[3])
            row = self.store.message(tag, int(parts[2]))
            if row is None:
                text = "Сообщение не найдено"
                markup = {
                    "inline_keyboard": [
                        [{"text": "↩️ Назад", "callback_data": f"s|{tag}|{page}"}]
                    ]
                }
            else:
                text, markup = detail_text(row, tag, page)
        else:
            await self.api.call("answerCallbackQuery", callback_query_id=callback_id)
            return

        await self.api.call("answerCallbackQuery", callback_query_id=callback_id)
        await self.render(chat_id, message_id, text, markup)
        if row is not None:
            asyncio.create_task(self.show_media(chat_id, parts[1], row))

    async def show_media(self, chat_id, tag, row):
        """Просит userbot отдать файл копии и отправляет его от имени бота."""
        copy_id = row["copy_id"]
        if not copy_id:
            return

        request_id = self.store.add_request(copy_id, chat_id)
        try:
            request = None
            deadline = time.time() + 15
            while time.time() < deadline:
                request = self.store.request(request_id)
                if request is None:
                    return
                if request["error"] or request["path"]:
                    break
                await asyncio.sleep(0.5)

            if request is None or request["error"]:
                log("media request failed:", request["error"] if request else "gone")
                return
            if not request["path"]:
                log("media request timed out")
                return

            path = request["path"]
            try:
                with open(path, "rb") as handle:
                    data = handle.read()
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass

            name, username = self.store.name(tag)
            caption = f"👤 <b>{esc(name)}</b>"
            if username:
                caption += f" · @{esc(username)}"
            caption += f" · {stamp(row['ts'])}"

            if row["kind"] in ("photo", "video"):
                is_photo = row["kind"] == "photo"
                try:
                    await self.api.upload(
                        "sendPhoto" if is_photo else "sendVideo",
                        "photo" if is_photo else "video",
                        chat_id,
                        data,
                        caption,
                        "photo.jpg" if is_photo else "video.mp4",
                    )
                    return
                except RuntimeError as exc:
                    log("media upload failed, sending as document:", exc)
            await self.api.upload(
                "sendDocument", "document", chat_id, data, caption, "file"
            )
        except Exception as exc:
            log("media failed:", type(exc).__name__, exc)
        finally:
            self.store.drop_request(request_id)


async def poll(api, menu):
    offset = 0
    while True:
        try:
            updates = await api.call(
                "getUpdates",
                offset=offset,
                timeout=50,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as exc:  # сеть, 409, 5xx — просто ждём
            log("poll failed:", type(exc).__name__, exc)
            await asyncio.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    await menu.on_message(update["message"])
                elif "callback_query" in update:
                    await menu.on_callback(update["callback_query"])
            except Exception as exc:
                log("update failed:", type(exc).__name__, exc)


async def main():
    api = Api(TOKEN)
    menu = Menu(api, Store(DB_PATH))
    log("menu bot started")
    try:
        await poll(api, menu)
    finally:
        await api.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
