#!/usr/bin/env python3
"""Userbot: в чат бота попадает только то, что исчезло.

Каждое входящее личное сообщение тихо учитывается: текст — в журнал, медиа —
копией-документом в CACHE-чате (по умолчанию «Избранное»). В TARGET (чат бота)
уходит только исчезнувшее:

- сообщение, которое собеседник удалил у всех (🗑);
- одноразовое фото/видео с таймером (🔥), которое исчезнет само.

Так архив в чате бота не забивается обычной перепиской.
"""

import asyncio
import html
import os
import re
import sqlite3
import sys
import time

from telethon import TelegramClient, events

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION = os.environ.get("SESSION", "session")
TARGET = os.environ.get("TARGET", "me")
CACHE = os.environ.get("CACHE", "me")
DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/tgsave")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
DB_PATH = os.path.join(DATA_DIR, "saved.db")

CAPTION_LIMIT = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS saved (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sender     TEXT,
    kind       TEXT,
    tag        TEXT,
    text       TEXT,
    copy_id    INTEGER,
    cache_chat INTEGER,
    surfaced   INTEGER NOT NULL DEFAULT 0,
    reason     TEXT,
    status     TEXT,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS edits (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    new_text   TEXT,
    ts         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS senders (
    tag        TEXT PRIMARY KEY,
    name       TEXT,
    username   TEXT,
    first_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    copy_id     INTEGER,
    chat_id     INTEGER,
    source_chat INTEGER,
    path        TEXT,
    error       TEXT,
    ts          INTEGER
);
"""


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


def stamp(ts):
    return time.strftime("%d.%m %H:%M", time.localtime(ts))


def display(entity):
    if entity is None:
        return "?"
    name = " ".join(
        p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p
    )
    return (
        name
        or getattr(entity, "title", None)
        or (f"@{entity.username}" if getattr(entity, "username", None) else None)
        or str(getattr(entity, "id", "?"))
    )


def sender_tag(sender):
    """Тег для поиска копий одного отправителя: #from_username / #from_id123."""
    username = getattr(sender, "username", None)
    raw = f"from_{username}" if username else f"from_id{getattr(sender, 'id', 'unknown')}"
    return re.sub(r"[^0-9A-Za-z_]", "_", raw).lower()[:40]


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()
        self._migrate()

    def _migrate(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(saved)")}
        types = {
            "tag": "TEXT",
            "text": "TEXT",
            "copy_id": "INTEGER",
            "cache_chat": "INTEGER",
            "surfaced": "INTEGER NOT NULL DEFAULT 0",
            "reason": "TEXT",
        }
        for name, sql_type in types.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE saved ADD COLUMN {name} {sql_type}")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(requests)")}
        if "source_chat" not in columns:
            self.db.execute("ALTER TABLE requests ADD COLUMN source_chat INTEGER")
        self.db.commit()

    def claim(self, chat_id, message_id, sender, kind, tag, text):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO saved "
            "(chat_id, message_id, sender, kind, tag, text, status, ts) "
            "VALUES (?,?,?,?,?,?,'tracked',?)",
            (chat_id, message_id, sender, kind, tag, text, int(time.time())),
        )
        self.db.commit()
        return cur.rowcount > 0

    def set_copy(self, chat_id, message_id, copy_id, cache_chat):
        self.db.execute(
            "UPDATE saved SET copy_id=?, cache_chat=? WHERE chat_id=? AND message_id=?",
            (copy_id, cache_chat, chat_id, message_id),
        )
        self.db.commit()

    def mark_surfaced(self, chat_id, message_id, reason):
        self.db.execute(
            "UPDATE saved SET surfaced=1, reason=?, status='shown' "
            "WHERE chat_id=? AND message_id=?",
            (reason, chat_id, message_id),
        )
        self.db.commit()

    def update_text(self, chat_id, message_id, text):
        self.db.execute(
            "UPDATE saved SET text=? WHERE chat_id=? AND message_id=?",
            (text, chat_id, message_id),
        )
        self.db.commit()

    def find(self, chat_id, message_id):
        return self.db.execute(
            "SELECT * FROM saved WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        ).fetchone()

    def find_by_id(self, message_id):
        rows = self.db.execute(
            "SELECT * FROM saved WHERE message_id=?", (message_id,)
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def add_sender(self, tag, name, username):
        cur = self.db.execute(
            "INSERT OR IGNORE INTO senders (tag, name, username, first_seen) "
            "VALUES (?,?,?,?)",
            (tag, name, username, int(time.time())),
        )
        self.db.commit()
        return cur.rowcount > 0

    def pending_requests(self):
        return self.db.execute(
            "SELECT id, copy_id, source_chat FROM requests "
            "WHERE path IS NULL AND error IS NULL"
        ).fetchall()

    def set_request_path(self, request_id, path):
        cur = self.db.execute(
            "UPDATE requests SET path=? WHERE id=?", (path, request_id)
        )
        self.db.commit()
        return cur.rowcount > 0

    def set_request_error(self, request_id, error):
        self.db.execute(
            "UPDATE requests SET error=? WHERE id=?", (error[:300], request_id)
        )
        self.db.commit()


def split_text(text, limit=3900):
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def trimmed(text, limit):
    body = html.escape(text)
    if len(body) <= limit:
        return body
    return body[: max(limit - 1, 0)] + "…"


class Saver:
    def __init__(self, client, journal, target_id, cache_id):
        self.client = client
        self.journal = journal
        self.target_id = target_id
        self.cache_id = cache_id

    def sender(self, tag):
        row = self.journal.db.execute(
            "SELECT name, username FROM senders WHERE tag=?", (tag,)
        ).fetchone()
        return (row["name"], row["username"]) if row else (tag, None)

    def head(self, tag, ts, label):
        name, username = self.sender(tag)
        line = f"{label} · <b>{html.escape(name or '?')}</b>"
        if username:
            line += f" · @{html.escape(username)}"
        line += f" · {stamp(ts)}"
        return f"{line}\n#{tag}"

    async def handle(self, event):
        msg = event.message
        chat_id = event.chat_id

        if chat_id == self.target_id or chat_id == self.cache_id:
            return
        if msg.action is not None:
            return

        sender = await event.get_sender()
        kind = self.kind(msg)
        tag = sender_tag(sender)
        text = msg.text or ""
        if not self.journal.claim(chat_id, msg.id, display(sender), kind, tag, text):
            return

        view_once = bool(getattr(msg.media, "ttl_seconds", None)) if msg.media else False
        self.journal.add_sender(tag, display(sender), getattr(sender, "username", None))

        if msg.media:
            path = await self.download(event)
            if path:
                caption = f"📥 <b>{html.escape(display(sender))}</b>"
                username = getattr(sender, "username", None)
                if username:
                    caption += f" · @{html.escape(username)}"
                caption += f" · {stamp(msg.date.timestamp())}\n#{tag}"
                if text:
                    room = CAPTION_LIMIT - len(caption) - 2
                    caption = f"{caption}\n\n{trimmed(text, room)}"
                try:
                    sent = await self.client.send_file(
                        CACHE,
                        path,
                        caption=caption,
                        parse_mode="html",
                        force_document=True,
                    )
                    self.journal.set_copy(chat_id, msg.id, sent.id, self.cache_id)
                finally:
                    os.remove(path)
            else:
                log(f"media {chat_id}/{msg.id} not downloaded")

        if view_once:
            row = self.journal.find(chat_id, msg.id)
            await self.surface(row, "view_once")

    async def surface(self, row, reason):
        if row is None or row["surfaced"]:
            return

        label = "🗑 <b>Удалено</b>" if reason == "deleted" else "🔥 <b>Одноразовое</b>"
        head = self.head(row["tag"], row["ts"], label)

        copy_id = row["copy_id"]
        if copy_id:
            source = row["cache_chat"] or self.cache_id
            try:
                cached = await self.client.get_messages(source, ids=copy_id)
            except Exception as exc:
                log(f"cache fetch failed: {type(exc).__name__}: {exc}")
                cached = None
            if cached is not None:
                await self.send_text(head)
                await self.client.forward_messages(TARGET, cached)
                try:
                    await self.client.delete_messages(source, [copy_id])
                except Exception:
                    pass
                self.journal.mark_surfaced(row["chat_id"], row["message_id"], reason)
                return

        text = row["text"] or ""
        body = f"\n\n{html.escape(text)}" if text else "\n\n<i>[копия недоступна]</i>"
        await self.send_text(f"{head}{body}")
        self.journal.mark_surfaced(row["chat_id"], row["message_id"], reason)

    async def download(self, event):
        try:
            return await event.download_media(file=TMP_DIR)
        except Exception as exc:
            log(f"download failed: {type(exc).__name__}: {exc}")
            return None

    async def send_text(self, text):
        sent = []
        for chunk in split_text(text):
            sent.append(
                await self.client.send_message(
                    TARGET, chunk, parse_mode="html", link_preview=False
                )
            )
        return sent

    @staticmethod
    def kind(msg):
        if msg.photo:
            return "photo"
        if msg.video:
            return "video"
        if msg.voice:
            return "voice"
        if msg.video_note:
            return "video_note"
        if msg.audio:
            return "audio"
        if msg.sticker:
            return "sticker"
        if msg.gif:
            return "gif"
        if msg.document:
            return "document"
        if msg.poll:
            return "poll"
        if msg.geo:
            return "location"
        return "text"


def prepare_dirs():
    os.makedirs(TMP_DIR, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    for name in os.listdir(TMP_DIR):
        try:
            os.remove(os.path.join(TMP_DIR, name))
        except OSError:
            pass


def secure_session():
    path = SESSION if SESSION.endswith(".session") else f"{SESSION}.session"
    if os.path.exists(path):
        os.chmod(path, 0o600)


async def serve_requests(client, journal, target):
    """Отдаёт меню-боту файлы копий: скачивает из кэша и кладёт путь в журнал."""
    while True:
        try:
            for request in journal.pending_requests():
                try:
                    source = request["source_chat"] or target.id
                    message = await client.get_messages(source, ids=request["copy_id"])
                    if message is None or not message.media:
                        raise RuntimeError("no media in copy")
                    path = await client.download_media(
                        message, file=os.path.join(TMP_DIR, f"req_{request['id']}")
                    )
                    if not journal.set_request_path(request["id"], path):
                        os.remove(path)
                        continue
                    log(f"media request {request['id']} ready")
                except Exception as exc:
                    log(f"media request {request['id']} failed: {type(exc).__name__}: {exc}")
                    journal.set_request_error(
                        request["id"], f"{type(exc).__name__}: {exc}"
                    )
        except Exception as exc:
            log("request loop failed:", type(exc).__name__, exc)
        await asyncio.sleep(1)


async def main():
    prepare_dirs()
    secure_session()

    journal = Journal(DB_PATH)
    client = TelegramClient(
        SESSION,
        API_ID,
        API_HASH,
        connection_retries=None,
        retry_delay=5,
        flood_sleep_threshold=600,
    )
    await client.connect()
    if not await client.is_user_authorized():
        log("сессия не авторизована — запусти login.py")
        return

    me = await client.get_me()
    target = await client.get_entity(TARGET)
    cache = await client.get_entity(CACHE)
    log(f"userbot @{me.username or me.id}: кэш {CACHE} (id {cache.id}), бот {TARGET} (id {target.id})")
    if cache.id == target.id:
        log("ВНИМАНИЕ: CACHE и TARGET совпадают — в чат будут попадать все сообщения")

    saver = Saver(client, journal, target.id, cache.id)
    asyncio.create_task(serve_requests(client, journal, target))

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_new(event):
        await saver.handle(event)

    @client.on(events.MessageEdited(incoming=True, func=lambda e: e.is_private))
    async def on_edit(event):
        if event.chat_id in (saver.target_id, saver.cache_id):
            return
        msg = event.message
        text = msg.text or ""
        journal.update_text(event.chat_id, msg.id, text)
        if msg.media:
            journal.db.execute(
                "UPDATE saved SET text=? WHERE chat_id=? AND message_id=?",
                (text, event.chat_id, msg.id),
            )
            journal.db.commit()

    @client.on(events.MessageDeleted)
    async def on_deleted(event):
        if event.chat_id in (saver.target_id, saver.cache_id):
            return
        for message_id in event.deleted_ids:
            row = journal.find(event.chat_id, message_id) if event.chat_id else None
            if row is None:
                row = journal.find_by_id(message_id)
            if row is not None:
                await saver.surface(row, "deleted")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
