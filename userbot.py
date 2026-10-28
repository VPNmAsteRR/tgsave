#!/usr/bin/env python3
"""Userbot: копирует входящие личные сообщения в заданный чат.

Работает под аккаунтом пользователя (MTProto), поэтому видит обычные личные
переписки — в отличие от бота, которому Telegram отдаёт только чаты с его
участием.

Медиа скачивается во временный файл, отправляется документом (байт-в-байт,
без пережатия) и сразу удаляется с диска. Одноразовые медиа сохраняются до
истечения таймера, правки приходят отдельным сообщением.
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
"""


def log(*args):
    print(time.strftime("%H:%M:%S"), *args, file=sys.stderr, flush=True)


def display(entity):
    """Человеческое имя отправителя для шапки копии."""
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
    """Тег для поиска всех копий одного отправителя.

    Telegram разрешает в хэштегах латиницу, цифры и подчёркивание, поэтому
    недопустимые символы заменяются, а тег всегда начинается с буквы.
    """
    username = getattr(sender, "username", None)
    raw = f"from_{username}" if username else f"from_id{getattr(sender, 'id', 'unknown')}"
    return re.sub(r"[^0-9A-Za-z_]", "_", raw).lower()[:40]


class Journal:
    """Учёт сохранённых сообщений: дедупликация и поиск для событий удаления."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.commit()
        self._migrate()

    def _migrate(self):
        """Добавляет колонки в журнал, созданный прошлой версией."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(saved)")}
        for name in ("tag", "text"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE saved ADD COLUMN {name} TEXT")
        self.db.commit()

    def claim(self, chat_id, message_id, sender, kind, tag, text):
        """Атомарно занять сообщение. False — уже обрабатывали."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO saved "
            "(chat_id, message_id, sender, kind, tag, text, status, ts) "
            "VALUES (?,?,?,?,?,?,'pending',?)",
            (chat_id, message_id, sender, kind, tag, text, int(time.time())),
        )
        self.db.commit()
        return cur.rowcount > 0

    def finish(self, chat_id, message_id, status):
        self.db.execute(
            "UPDATE saved SET status=? WHERE chat_id=? AND message_id=?",
            (status, chat_id, message_id),
        )
        self.db.commit()

    def find(self, chat_id, message_id):
        return self.db.execute(
            "SELECT * FROM saved WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        ).fetchone()

    def update_text(self, chat_id, message_id, text):
        self.db.execute(
            "UPDATE saved SET text=? WHERE chat_id=? AND message_id=?",
            (text, chat_id, message_id),
        )
        self.db.commit()

    def add_edit(self, chat_id, message_id, text):
        self.db.execute(
            "INSERT INTO edits (chat_id, message_id, new_text, ts) VALUES (?,?,?,?)",
            (chat_id, message_id, text, int(time.time())),
        )
        self.db.commit()

    def add_sender(self, tag, name, username):
        """True — отправитель встретился впервые."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO senders (tag, name, username, first_seen) VALUES (?,?,?,?)",
            (tag, name, username, int(time.time())),
        )
        self.db.commit()
        return cur.rowcount > 0


def split_text(text, limit=3900):
    """Режет длинный текст под лимит Telegram, не разрывая строки без нужды."""
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
    """HTML-escaped текст, обрезанный под лимит с многоточием."""
    body = html.escape(text)
    if len(body) <= limit:
        return body
    return body[: max(limit - 1, 0)] + "…"


class Saver:
    def __init__(self, client, journal, target_id):
        self.client = client
        self.journal = journal
        self.target_id = target_id

    async def header(self, event, edited=False):
        sender = await event.get_sender()
        stamp = time.strftime("%d.%m %H:%M", time.localtime(event.message.date.timestamp()))
        line = f"📥 <b>{html.escape(display(sender))}</b>"
        username = getattr(sender, "username", None)
        if username:
            line += f" · @{html.escape(username)}"
        line += f" · {stamp}"
        if edited:
            line += " · ✏️ изменено"
        return f"{line}\n#{sender_tag(sender)}"

    async def handle(self, event):
        msg = event.message
        chat_id = event.chat_id

        # Не копируем то, что и так лежит в целевом чате: иначе бот с
        # автоответом и TARGET на его чат дают бесконечную петлю.
        if chat_id == self.target_id:
            return
        if msg.action is not None:
            return

        sender = await event.get_sender()
        kind = self.kind(msg)
        tag = sender_tag(sender)
        if not self.journal.claim(chat_id, msg.id, display(sender), kind, tag, msg.text or ""):
            return

        head = await self.header(event)
        try:
            if self.journal.add_sender(
                tag, display(sender), getattr(sender, "username", None)
            ):
                await self.send_text(
                    f"📇 <b>{html.escape(display(sender))}</b> → #{tag}\n"
                    f"<i>тап по тегу — все копии от него</i>"
                )
            if msg.media:
                path = await self.download(event)
                if path:
                    caption = head
                    if msg.text:
                        room = CAPTION_LIMIT - len(head) - 2
                        caption = f"{head}\n\n{trimmed(msg.text, room)}"
                    try:
                        await self.client.send_file(
                            TARGET,
                            path,
                            caption=caption,
                            parse_mode="html",
                            force_document=True,
                        )
                    finally:
                        os.remove(path)
                else:
                    await self.send_text(f"{head}\n\n<i>[{kind}: файл не скачался]</i>")
            elif msg.text:
                await self.send_text(f"{head}\n\n{html.escape(msg.text)}")
            else:
                await self.send_text(f"{head}\n\n<i>[{kind}]</i>")
            self.journal.finish(chat_id, msg.id, "sent")
        except Exception as exc:
            log(f"save {chat_id}/{msg.id} failed: {type(exc).__name__}: {exc}")
            self.journal.finish(chat_id, msg.id, f"error:{type(exc).__name__}")
            try:
                await self.send_text(
                    f"{head}\n\n<i>[не сохранено: {html.escape(str(exc))[:200]}]</i>"
                )
            except Exception:
                pass

    async def handle_edit(self, event):
        if event.chat_id == self.target_id:
            return
        msg = event.message
        head = await self.header(event, edited=True)
        text = msg.text or ""
        self.journal.add_edit(event.chat_id, msg.id, text)
        self.journal.update_text(event.chat_id, msg.id, text)
        if text:
            await self.send_text(f"{head}\n\n{html.escape(text)}")
        else:
            await self.send_text(f"{head}\n\n<i>[подпись изменена]</i>")

    async def download(self, event):
        try:
            return await event.download_media(file=TMP_DIR)
        except Exception as exc:
            log(f"download failed: {type(exc).__name__}: {exc}")
            return None

    async def send_text(self, text):
        for chunk in split_text(text):
            await self.client.send_message(
                TARGET, chunk, parse_mode="html", link_preview=False
            )

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
    log(f"userbot @{me.username or me.id} -> {TARGET} (id {target.id})")

    saver = Saver(client, journal, target.id)

    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_new(event):
        await saver.handle(event)

    @client.on(events.MessageEdited(incoming=True, func=lambda e: e.is_private))
    async def on_edit(event):
        await saver.handle_edit(event)

    @client.on(events.MessageDeleted)
    async def on_deleted(event):
        if event.chat_id is None:
            return
        for message_id in event.deleted_ids:
            if journal.find(event.chat_id, message_id):
                log(f"deleted {event.chat_id}/{message_id} (копия сохранена)")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
